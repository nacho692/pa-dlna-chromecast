#! /bin/env python
"""An Upnp control point that forwards Pulseaudio streams to DLNA devices.
"""

import sys
import argparse
import ipaddress
import subprocess
import json
import logging
import asyncio
import itertools
from signal import SIGINT, SIGTERM, strsignal
from pa_dlna import __version__
from pa_dlna.upnp import Upnp
from pa_dlna.pulseaudio import Pulseaudio

logger = logging.getLogger('pa-dlna')

def networks_option(ip_list, parser):
    """Return a list of IPv4 addresses from a comma separated list.

    IP_LIST is a comma separated list of the local IPv4 addresses where DLNA
    devices may be discovered; when this option is an empty string or the
    '--networks' option string is missing, all local addresses are used
    except 127.0.0.1
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
        try:
            proc = subprocess.run(
              ['ip', '-family', 'inet', '-brief', '-json', 'address', 'show'],
              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError as e:
            parser.error('ip command not available, use the --networks option')
        try:
            json_out = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            parser.error(f'json loads exception in {proc.stdout}: {e}')

        addresses = []
        for item in json_out:
            ip = item['addr_info'][0]['local']
            if ip != '127.0.0.1':
                addresses.append(ip)

    if not addresses:
        parser.error('no local IPv4 address')
    return addresses

def parse_args():
    """Parse the command line."""

    def _networks_option(ip_list):
        return networks_option(ip_list, parser)

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
                        type=_networks_option,
                        help=' '.join(line.strip() for line in
                                     networks_option.__doc__.split('\n')[2:]))
    parser.add_argument('--ttl', type=int, default=2,
                        help='the IP packets time to live '
                        '(default: %(default)s)')
    return vars(parser.parse_args())

class PaDLNA:
    """Manage the upnp_t and pulseaudio_t asyncio tasks."""

    def __init__(self, options):
        self.options = options
        self.logfile_hdler = None
        self.setup_logging()

    def setup_logging(self):
        logging.basicConfig(
            level=getattr(logging, self.options['loglevel'].upper()),
            format='%(name)-7s %(levelname)-7s %(message)s')

        # Add a file handler set at the debug level.
        if self.options['logfile'] is not None:
            try:
                logfile_hdler = logging.FileHandler(self.options['logfile'],
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
                self.logfile_hdler = logfile_hdler

    def shutdown_logging(self):
        if self.logfile_hdler is not None:
            logging.getLogger().removeHandler(self.logfile_hdler)
            self.logfile_hdler.close()

    def sig_handler(self, signal, pending):
        logger.info(f'got signal {strsignal(signal)}')
        for t in pending:
            t.cancel()

    async def cancel_tasks(self, done, pending):
        for t in pending:
            t.cancel()
        await asyncio.sleep(0)
        for t in itertools.chain(done, pending):
            state = ('cancelled' if t.cancelled()
                     else f'done, result: {t.result()}')
            logger.info(f'task {t.get_name()}: {state}')
        self.shutdown_logging()

    async def run(self):
        """Start the upnp_t and pulseaudio_t asyncio tasks."""

        logger.info(f'Starting pa-dlna')
        logger.info(f'Options {self.options}')

        upnp = Upnp(self.options['networks'], self.options['ttl'])
        upnp_t = asyncio.create_task(upnp.run(), name='upnp_t')
        pulseaudio_t = asyncio.create_task(Pulseaudio(upnp.queue).run(),
                                                name='pulseaudio_t')

        # Set up signal handlers.
        loop = asyncio.get_running_loop()
        for s in (SIGINT, SIGTERM):
            loop.add_signal_handler(s,
                lambda s=s: self.sig_handler(s, (upnp_t, pulseaudio_t)))

        done, pending = await asyncio.wait((upnp_t, pulseaudio_t),
                                           return_when=asyncio.FIRST_COMPLETED)
        await self.cancel_tasks(done, pending)

def main():
    if sys.version_info.major != 3 or sys.version_info.minor < 7:
        print('error: pa-dlna: the python version must be at least 3.7',
              file=sys.stderr)
        sys.exit(1)

    options = parse_args()
    try:
        padlna = PaDLNA(options)
        asyncio.run(padlna.run())
    finally:
        padlna.shutdown_logging()

if __name__ == '__main__':
    main()
