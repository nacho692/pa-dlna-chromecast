#! /bin/env python
"""An UPnP control point that forwards Pulseaudio streams to DLNA devices.
"""

import sys
import argparse
import ipaddress
import subprocess
import json
import logging
import asyncio
from signal import SIGINT, SIGTERM, strsignal
from pa_dlna import __version__
from pa_dlna.upnp import AsyncioTasks
from pa_dlna.pulseaudio import Pulseaudio

logger = logging.getLogger('pa-dlna')

def setup_logging(options):
    logging.basicConfig(
        level=getattr(logging, options['loglevel'].upper()),
        format='%(name)-7s %(levelname)-7s %(message)s')

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
        logger.debug(f'output of "{cmd}"\n:{json_out}')
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
    parser.add_argument('--loglevel', '-l', default='warning',
                        choices=('debug', 'info', 'warning', 'error'),
                        help='set the log level of the logging console on '
                        'stderr (default: %(default)s)')
    parser.add_argument('--logfile', '-f', metavar='FILE',
                        help='FILE is the pathname of a logging file '
                        'handler set at the debug log level')
    parser.add_argument('--networks', '-n', metavar="IP_LIST", default='',
                        help=' '.join(line.strip() for line in
                                     networks_option.__doc__.split('\n')[2:]))
    parser.add_argument('--ttl', type=int, default=2,
                        help='the IP packets time to live '
                        '(default: %(default)s)')

    # Options as a dict.
    options = vars(parser.parse_args())

    logfile_hdler = setup_logging(options)
    logger.info(f'Starting pa-dlna')

    # Run networks_option() once logging has been setup.
    options['networks'] = networks_option(options['networks'], parser)

    return options, logfile_hdler

class PaDLNA:
    """Manage the pulseaudio task."""

    def __init__(self, options):
        self.options = options          # a dict
        self.closed = False
        self.aio_tasks = AsyncioTasks()

    def sig_handler(self, signal):
        logger.info(f'Got signal {strsignal(signal)}')
        self.close()

    async def run(self):
        """Start the pulseaudio task."""

        loop = asyncio.get_running_loop()
        try:
            pulseaudio_t = self.aio_tasks.create_task(
                Pulseaudio(self.options['networks'],
                           self.options['ttl']).run(), name='pulseaudio')

            # Set up signal handlers.
            for s in (SIGINT, SIGTERM):
                loop.add_signal_handler(s, lambda s=s: self.sig_handler(s))

            await asyncio.wait((pulseaudio_t, ),
                                        return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.aio_tasks.cancel_all()
            logger.info('End of pa-dlna')

def main():
    if sys.version_info.major != 3 or sys.version_info.minor < 7:
        print('error: pa-dlna: the python version must be at least 3.7',
              file=sys.stderr)
        sys.exit(1)

    options, logfile_hdler = parse_args()
    logger.info(f'Options {options}')

    padlna = PaDLNA(options)
    try:
        asyncio.run(padlna.run())
    finally:
        if logfile_hdler is not None:
            logfile_hdler.flush()
        logging.shutdown()

if __name__ == '__main__':
    main()
