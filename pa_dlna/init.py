"""Utilities for starting an UPnPApplication."""

import sys
import os
import argparse
import logging
import asyncio
import threading
import struct

from . import __version__
from .config import DefaultConfig, UserConfig

logger = logging.getLogger('init')

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
    """Parse the command line."""

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

    parser = argparse.ArgumentParser(description=doc)
    prog = 'pa-dlna' if pa_dlna else 'upnp-cmd'
    parser.prog = prog
    parser.add_argument('--version', '-v', action='version',
                        version='%(prog)s: version ' + __version__)
    parser.add_argument('--nics', '-n', default='',
                        help='NICS is a comma separated list of the names of'
                        ' network interface controllers where UPnP devices'
                        " may be discovered, such as 'wlan0,enp5s0' for"
                        ' example. All the interfaces are used when this'
                        ' option is an empty string or the option is missing'
                        " (default: '%(default)s')")
    parser.add_argument('--msearch-interval', '-m', type=int, default=60,
                        help='set the time interval in seconds between the'
                        ' sending of the MSEARCH datagrams used for device'
                        ' discovery (default: %(default)s)')
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
    parser.add_argument('--log-aio', '-a', action='store_true',
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
