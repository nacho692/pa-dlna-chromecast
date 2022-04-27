"""Redirect pulseaudio streams to DLNA MediaRenderers."""

import sys
import argparse
import ipaddress
import subprocess
import json
import logging
import asyncio
import re
from . import __version__

from .upnp import (UPnPControlPoint, UPnPClosedDeviceError, AsyncioTasks,
                   UPnPSoapFaultError)

logger = logging.getLogger('pa-dlna')

MIN_PYTHON_VERSION = (3, 7)
MEDIARENDERER = 'urn:schemas-upnp-org:device:MediaRenderer:'
AVTRANSPORT = 'urn:upnp-org:serviceId:AVTransport'
RENDERINGCONTROL = 'urn:upnp-org:serviceId:RenderingControl'
CONNECTIONMANAGER = 'urn:upnp-org:serviceId:ConnectionManager'

# Parsing arguments utilities.
class FilterDebug:

    def filter(self, record):
        """Ignore DEBUG logging messages."""
        if record.levelno != logging.DEBUG:
            return True

def setup_logging(options):
    logging.basicConfig(
        level=getattr(logging, options['loglevel'].upper()),
        format='%(name)-7s %(levelname)-7s %(message)s')

    if not options['logaio']:
        logging.getLogger('asyncio').addFilter(FilterDebug())

    # Add a file handler set at the debug level.
    if options['logfile'] is not None:
        try:
            logfile_hdler = logging.FileHandler(options['logfile'],
                                                mode='w')
        except IOError as e:
            logging.error(f'cannot setup the log file: {e}')
        else:
            logfile_hdler.setLevel(logging.DEBUG)
            formatter = logging.Formatter(
                fmt='%(asctime)s %(name)-7s %(levelname)-7s %(message)s',
                datefmt='%m-%d %H:%M.%S')
            logfile_hdler.setFormatter(formatter)
            logging.getLogger().addHandler(logfile_hdler)
            return logfile_hdler

    return None

def networks_option(ip_list, parser):
    """Return a list of IPv4 addresses from a comma separated list.

    IP_LIST is a comma separated list of the local IPv4 addresses of the
    network interfaces where DLNA devices may be discovered.
    When this option is an empty string or the option is missing, the first
    local address of each network interface is used, except 127.0.0.1.
    """

    if ip_list:
        addresses = list(x.strip() for x in ip_list.split(','))
        # Check addresses validity.
        for ip in addresses:
            try:
                if not isinstance(ipaddress.ip_address(ip),
                                  ipaddress.IPv4Address):
                    parser.error(f'{ip} not an IPv4Address')
            except ValueError as e:
                parser.error(e)
    else:
        # Use the ip command to get the list of local IPv4 addresses.
        cmd = 'ip -family inet -brief -json address show'
        try:
            proc = subprocess.run(cmd.split(),
              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError as e:
            parser.error('ip command not available, use the --networks option')
        try:
            json_out = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            parser.error(f'json loads exception in {proc.stdout}: {e}')

        addresses = []
        logger.debug(f'Output of "{cmd}"\n:{json_out}')
        for item in json_out:
            for addr in item['addr_info']:
                ip = addr['local']
                if ip != '127.0.0.1':
                    addresses.append(ip)
                break                   # only one local address is needed per
                                        # network interface

    if not addresses:
        parser.error('no network interface available')
    return addresses

def parse_args():
    """Parse the command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', '-v', action='version',
                        version='%(prog)s: version ' + __version__)
    parser.add_argument('--networks', '-n', metavar="IP_LIST", default='',
                        help=' '.join(line.strip() for line in
                                     networks_option.__doc__.split('\n')[2:]))
    parser.add_argument('--ttl', type=int, default=2,
                        help='the IP packets time to live '
                        '(default: %(default)s)')
    parser.add_argument('--loglevel', '-l', default='warning',
                        choices=('debug', 'info', 'warning', 'error'),
                        help='set the log level of the logging console on '
                        'stderr (default: %(default)s)')
    parser.add_argument('--logfile', '-f', metavar='FILE',
                        help='FILE is the pathname of a logging file '
                        "handler set at the 'debug' log level")
    parser.add_argument('--logaio', '-a', action='store_true',
                        help='do not ignore asyncio log entries at the'
                        " 'debug' log level. The default is to ignore those"
                        ' verbose logs.')

    # Options as a dict.
    options = vars(parser.parse_args())

    logfile_hdler = setup_logging(options)
    logger.info(f'Starting pa-dlna')

    # Run networks_option() once logging has been setup.
    options['networks'] = networks_option(options['networks'], parser)

    return options, logfile_hdler

# Class(es).
class MediaRenderer:
    """A DLNA MediaRenderer.

    See the Standardized DCP (SDCP) specifications:
      UPnP AV Architecture:2
      AVTransport:3 Service
      RenderingControl:3 Service
      ConnectionManager:3 Service
    """

    def __init__(self, root_device, padlna):
        self.root_device = root_device
        self.padlna = padlna

    def close(self):
        self.root_device.close()

    async def soap_action(self, serviceId, action, args):
        """Send a SOAP action.

        Return the dict {argumentName: out arg value} if successfull,
        otherwise an instance of the upnp.xml.SoapFault namedtuple defined by
        field names in ('errorCode', 'errorDescription').
        """

        try:
            service = self.root_device.serviceList[serviceId]
            return await service.soap_action(action, args)
        except UPnPSoapFaultError as e:
            return e.args[0]
        except Exception as e:
            logger.exception(f'{e!r}')
            self.close()

    async def run(self):
        """Set up the MediaRenderer."""

        resp = await self.soap_action(AVTRANSPORT, 'GetMediaInfo',
                                      {'InstanceID': 0})

class PaDlna:
    """Manage the DLNA MediaRenderer devices and Pulseaudio."""

    def __init__(self, ipaddr_list, ttl):
        self.ipaddr_list = ipaddr_list
        self.ttl = ttl
        self.closed = False
        self.devices = {}               # dict {UpnpDevice: MediaRenderer}
        self.aio_tasks = AsyncioTasks()

    def close(self):
        if not self.closed:
            self.closed = True
            for renderer in self.devices.values():
                renderer.close()
            self.devices = {}
            self.aio_tasks.cancel_all()

    def remove_device(self, device):
        if device in self.devices:
            del self.devices[device]

    async def run(self):
        try:
            # Run the UPnP control point.
            async with UPnPControlPoint(self.ipaddr_list, self.ttl) as upnp:
                while True:
                    notification, root_device = await upnp.get_notification()
                    logger.info(f'Got notification'
                                f' {(notification, root_device)}')

                    # Ignore non MediaRenderer devices.
                    if re.match(rf'{MEDIARENDERER}(1|2)',
                                root_device.deviceType) is None:
                        continue

                    if notification == 'alive':
                        if root_device not in self.devices:
                            renderer = MediaRenderer(root_device, self)
                            await renderer.run()
                            self.devices[root_device] = renderer
                    else:
                        self.remove_device(root_device)

        except Exception as e:
            logger.exception(f'Got exception {e!r}')
        except asyncio.CancelledError as e:
            logger.error(f'Got exception {e!r}')
        finally:
            self.close()

# The main function.
def main():
    ver = sys.version_info
    if ver[0] != MIN_PYTHON_VERSION[0] or ver < MIN_PYTHON_VERSION:
        print(f'error: pa-dlna: the python version must be at least'
              f' {MIN_PYTHON_VERSION}', file=sys.stderr)
        sys.exit(1)

    # Parse options.
    options, logfile_hdler = parse_args()
    logger.info(f'Options {options}')

    # Run the PaDlna instance.
    pa_dlna = PaDlna(options['networks'], options['ttl'])
    try:
        asyncio.run(pa_dlna.run())
    except asyncio.CancelledError as e:
        logger.error(f'Got exception {e!r}')
    finally:
        logger.info('End of pa-dlna')
        if logfile_hdler is not None:
            logfile_hdler.flush()
        logging.shutdown()

if __name__ == '__main__':
    main()
