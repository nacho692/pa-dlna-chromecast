"""Utilities for starting an UPnPApplication."""

import sys
import argparse
import ipaddress
import subprocess
import json
import logging
import asyncio
import threading

__version__ = '0.1'
MIN_PYTHON_VERSION = (3, 7)

ver = sys.version_info
if ver[0] != MIN_PYTHON_VERSION[0] or ver < MIN_PYTHON_VERSION:
    print(f'error: the python version must be at least'
          f' {MIN_PYTHON_VERSION}', file=sys.stderr)
    sys.exit(1)

# Parsing arguments utilities.
class FilterDebug:

    def filter(self, record):
        """Ignore DEBUG logging messages."""
        if record.levelno != logging.DEBUG:
            return True

def setup_logging(options):

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    stream_hdler = logging.StreamHandler()
    stream_hdler.setLevel(getattr(logging, options['loglevel'].upper()))
    formatter = logging.Formatter(fmt='%(name)-7s %(levelname)-7s %(message)s')
    stream_hdler.setFormatter(formatter)
    root.addHandler(stream_hdler)

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
            root.addHandler(logfile_hdler)
            return logfile_hdler

    return None

def networks_option(ip_list, parser, logger):
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
            parser.error("'ip' command not available, use the --networks"
                         ' option')
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

def parse_args(doc, logger):
    """Parse the command line."""

    parser = argparse.ArgumentParser(description=doc)
    parser.add_argument('--version', '-v', action='version',
                        version='%(prog)s: version ' + __version__)
    parser.add_argument('--networks', '-n', metavar="IP_LIST", default='',
                        dest='ip_list',
                        help=' '.join(line.strip() for line in
                                     networks_option.__doc__.split('\n')[2:]))
    parser.add_argument('--ttl', type=int, default=2,
                        help='the IP packets time to live '
                        '(default: %(default)s)')
    parser.add_argument('--loglevel', '-l', default='error',
                        choices=('debug', 'info', 'warning', 'error'),
                        help='set the log level of the logging console on '
                        'stderr (default: %(default)s)')
    parser.add_argument('--logfile', '-f', metavar='FILE',
                        help='FILE is the pathname of a logging file '
                        "handler set at the 'debug' log level")
    parser.add_argument('--logaio', '-a', action='store_true',
                        help='do not ignore asyncio log entries at the'
                        " 'debug' log level; the default is to ignore those"
                        ' verbose logs')

    # Options as a dict.
    options = vars(parser.parse_args())

    logfile_hdler = setup_logging(options)

    # Run networks_option() once logging has been setup.
    options['ip_list'] = networks_option(options['ip_list'], parser,
                                             logger)

    return options, logfile_hdler

# Class.
class UPnPApplication:
    """An UpnP application."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    async def run_control_point(self):
        raise NotImplementedError

    def __str__(self):
        raise NotImplementedError

# The main function.
def main_function(clazz, doc, logger, inthread=False):

    def run_in_thread(coro):
        """Run the UPnP control point in a thread."""

        cp_thread = threading.Thread(target=asyncio.run, args=[coro])
        cp_thread.start()
        return cp_thread

    assert issubclass(clazz, UPnPApplication)

    # Parse options.
    options, logfile_hdler = parse_args(doc, logger)
    logger.info(f'Options {options}')

    # Run the UPnPApplication instance.
    app = clazz(**options)
    logger.info(f'Starting {app}')
    try:
        if inthread:
            event = threading.Event()
            cp_thread = run_in_thread(app.run_control_point(event))
            app.run(cp_thread, event)
        else:
            asyncio.run(app.run_control_point())
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt as e:
        logger.info(f'{app} got {e!r}')
    finally:
        logger.info(f'End of {app}')
        if logfile_hdler is not None:
            logfile_hdler.flush()
        logging.shutdown()
