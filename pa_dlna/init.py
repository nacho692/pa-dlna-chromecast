"""Utilities for starting an UPnPApplication."""

import sys
import os
import argparse
import ipaddress
import logging
import asyncio
import threading
import struct
import atexit
try:
    import termios
except ImportError:
    termios = None

from . import __version__
from .config import DefaultConfig, UserConfig

logger = logging.getLogger('init')

def disable_xonxoff(fd):
    """Disable XON/XOFF flow control on output."""

    def restore_termios():
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old_attr)
        except termios.error as e:
            print(f'Error failing to restore termios: {e!r}', file=sys.stderr)

    if termios is not None and os.isatty(fd):
        try:
            old_attr = termios.tcgetattr(fd)
            new_attr = termios.tcgetattr(fd)
            new_attr[0] = new_attr[0] & ~termios.IXON
            termios.tcsetattr(fd, termios.TCSANOW, new_attr)
            logger.debug('Disabling XON/XOFF flow control on output')
            return restore_termios
        except termios.error:
            pass

# Parsing arguments utilities.
class FilterDebug:

    def filter(self, record):
        """Ignore DEBUG logging messages."""
        if record.levelno != logging.DEBUG:
            return True

def setup_logging(options, loglevel='warning'):

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    options_loglevel = options.get('loglevel')
    loglevel = options_loglevel if options_loglevel else 'error'
    stream_hdler = logging.StreamHandler()
    stream_hdler.setLevel(getattr(logging, loglevel.upper()))
    formatter = logging.Formatter(fmt='%(name)-7s %(levelname)-7s %(message)s')
    stream_hdler.setFormatter(formatter)
    root.addHandler(stream_hdler)

    if options['nolog_upnp']:
        logging.getLogger('upnp').addFilter(FilterDebug())
        logging.getLogger('network').addFilter(FilterDebug())
    if not options['log_aio']:
        logging.getLogger('asyncio').addFilter(FilterDebug())

    # Add a file handler set at the debug level.
    if options['logfile'] is not None:
        logfile = os.path.expanduser(options['logfile'])
        try:
            logfile_hdler = logging.FileHandler(logfile, mode='w')
        except OSError as e:
            logging.error(f'cannot setup the log file: {e!r}')
        else:
            logfile_hdler.setLevel(logging.DEBUG)
            formatter = logging.Formatter(
                fmt='%(asctime)s %(name)-7s %(levelname)-7s %(message)s')
            logfile_hdler.setFormatter(formatter)
            root.addHandler(logfile_hdler)
            return logfile_hdler

    return None

def parse_args(doc, pa_dlna=True, argv=sys.argv[1:]):
    """Parse the command line.

    UPnP discovery is run on all the networks (except the loopbak interface
    'lo') when the '--ip-addresses' and '--nics' command line arguments are
    not used or empty. Otherwise both arguments may be used indifferently or
    even jointly.
    """

    def pack_B(ttl):
        try:
            ttl = int(ttl)
            return struct.pack('B', ttl)
        except (struct.error, ValueError) as e:
            parser.error(f"Bad 'ttl' argument: {e!r}")

    def mime_types(mtypes):
        mtypes = [y for y in (x.strip() for x in mtypes.split(',')) if y]
        if len(set(mtypes)) != len(mtypes):
            parser.error('The mime types in MIME-TYPES must be different')
        for mtype in mtypes:
            mtype_split = mtype.split('/')
            if len(mtype_split) != 2 or mtype_split[0] != 'audio':
                parser.error(f"'{mtype}' is not an audio mime type")
        return mtypes

    def ipv4_addresses(ip_addresses):
        ipv4_addrs = []
        for addr in (x.strip() for x in ip_addresses.split(',')):
            if addr:
                try:
                    ipaddress.IPv4Address(addr)
                except ValueError as e:
                    parser.error(e)
                ipv4_addrs.append(addr)
        return ipv4_addrs

    parser = argparse.ArgumentParser(description=doc,
                        epilog=' '.join(parse_args.__doc__.split('\n')[2:]))
    prog = 'pa-dlna' if pa_dlna else 'upnp-cmd'
    parser.prog = prog
    parser.add_argument('--version', '-v', action='version',
                        version='%(prog)s: version ' + __version__)
    parser.add_argument('--ip-addresses', '-a', default='',
                        type=ipv4_addresses,
                        help='IP_ADDRESSES is a comma separated list of the'
                        ' local IPv4 addresses of the networks where UPnP'
                        " devices may be discovered (default: '%(default)s')")
    parser.add_argument('--nics', '-n', default='',
                        help='NICS is a comma separated list of the names of'
                        ' network interface controllers where UPnP devices'
                        " may be discovered such as 'wlan0,enp5s0' for"
                        " example (default: '%(default)s')")
    parser.add_argument('--msearch-interval', '-m', type=int, default=60,
                        help='set the time interval in seconds between the'
                        ' sending of the MSEARCH datagrams used for UPnP'
                        ' device discovery (default: %(default)s)')
    parser.add_argument('--msearch-port', '-p', type=int, default=0,
                        help='set the local UDP port for receiving MSEARCH'
                        ' response messages from UPnP devices, a value of'
                        " '0' means letting the operating system choose an"
                        ' ephemeral port (default: %(default)s)')
    parser.add_argument('--ttl', type=pack_B, default=b'\x02',
                        help='set the IP packets time to live to TTL'
                        ' (default: 2)')
    if pa_dlna:
        parser.add_argument('--port', type=int, default=8080,
                            help='set the TCP port on which the HTTP server'
                            ' handles DLNA requests (default: %(default)s)')
        parser.add_argument('--dump-default', '-d', action='store_true',
                            help='write to stdout (and exit) the default'
                            ' built-in configuration')
        parser.add_argument('--dump-internal', '-i', action='store_true',
                            help='write to stdout (and exit) the'
                            ' configuration used internally by the program on'
                            ' startup after the pa-dlna.conf user'
                            ' configuration file has been parsed')
        parser.add_argument('--loglevel', '-l', default='info',
                            choices=('debug', 'info', 'warning', 'error'),
                            help='set the log level of the stderr logging'
                            ' console (default: %(default)s)')
    parser.add_argument('--logfile', '-f', metavar='PATH',
                        help='add a file logging handler set at '
                        "'debug' log level whose path name is PATH")
    parser.add_argument('--nolog-upnp', '-u', action='store_true',
                        help="ignore UPnP log entries at 'debug' log level")
    parser.add_argument('--log-aio', '-y', action='store_true',
                        help='do not ignore asyncio log entries at'
                        " 'debug' log level; the default is to ignore those"
                        ' verbose logs')
    if pa_dlna:
        parser.add_argument('--test-devices', '-t', metavar='MIME-TYPES',
                            type=mime_types, default='',
                            help='MIME-TYPES is a comma separated list of'
                            ' distinct audio mime types. A DLNATestDevice is'
                            ' instantiated for each one of these mime types'
                            ' and registered as a virtual DLNA device. Mostly'
                            ' for testing.')

    # Options as a dict.
    options = vars(parser.parse_args(argv))

    dump_default = options.get('dump_default')
    dump_internal = options.get('dump_internal')
    if dump_default and dump_internal:
        parser.error(f"Cannot set both '--dump-default' and "
                     f"'--dump-internal' arguments simultaneously")
    if dump_default or dump_internal:
        return options, None

    logfile_hdler = setup_logging(options)
    if options['logfile'] is not None and logfile_hdler is None:
        logging.shutdown()
        sys.exit(2)

    logger.info('pa-dlna version ' + __version__)
    logger.info('Python version ' + sys.version)
    options['nics'] = [nic for nic in
                       (x.strip() for x in options['nics'].split(',')) if nic]
    logger.info(f'Options {options}')
    return options, logfile_hdler

# Classes.
class ControlPointAbortError(Exception): pass
class UPnPApplication:
    """An UPnP application."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    async def run_control_point(self):
        raise NotImplementedError

    def __str__(self):
        raise NotImplementedError

# The main function.
def padlna_main(clazz, doc, argv=sys.argv):

    def run_in_thread(coro):
        """Run the UPnP control point in a thread."""

        cp_thread = threading.Thread(target=asyncio.run, args=[coro])
        cp_thread.start()
        return cp_thread

    assert clazz.__name__ in ('AVControlPoint', 'UPnPControlCmd')
    pa_dlna = True if clazz.__name__ == 'AVControlPoint' else False

    # Parse the arguments.
    options, logfile_hdler = parse_args(doc, pa_dlna, argv[1:])

    # Instantiate the UPnPApplication.
    if pa_dlna:
        # Get the encoders configuration.
        try:
            if options['dump_default']:
                DefaultConfig().write(sys.stdout)
                sys.exit(0)

            config = UserConfig()
            if options['dump_internal']:
                config.print_internal_config()
                sys.exit(0)
        except Exception as e:
            logger.error(f'{e!r}')
            sys.exit(1)
        app = clazz(config=config, **options)
    else:
        app = clazz(**options)

    # Run the UPnPApplication instance.
    logger.info(f'Start {app}')
    exit_code = 1
    try:
        if pa_dlna:
            fd = sys.stdin.fileno()
            restore_termios = disable_xonxoff(fd)
            if restore_termios is not None:
                atexit.register(restore_termios)
            exit_code = asyncio.run(app.run_control_point())
        else:
            # Run the control point of upnp-cmd in a thread.
            event = threading.Event()
            cp_thread = run_in_thread(app.run_control_point(event))
            exit_code = app.run(cp_thread, event)
    except KeyboardInterrupt as e:
        logger.info(f'{app} got {e!r}')
    finally:
        logger.info(f'End of {app}')
        if logfile_hdler is not None:
            logfile_hdler.flush()
        logging.shutdown()
        sys.exit(exit_code)
