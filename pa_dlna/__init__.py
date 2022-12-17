"""Utilities for starting an UPnPApplication."""

import sys
import os
import argparse
import ipaddress
import subprocess
import json
import logging
import asyncio
import threading
import struct

from .config import DefaultConfig, UserConfig
from . import encoders as encoders_module

__version__ = '0.1'
MIN_PYTHON_VERSION = (3, 8)

VERSION = sys.version_info
if VERSION[:2] < MIN_PYTHON_VERSION:
    print(f'error: the python version must be at least'
          f' {MIN_PYTHON_VERSION}', file=sys.stderr)
    sys.exit(1)

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
    if options_loglevel is not None:
        loglevel = options_loglevel
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
        except IOError as e:
            logging.error(f'cannot setup the log file: {e}')
        else:
            logfile_hdler.setLevel(logging.DEBUG)
            formatter = logging.Formatter(
                fmt='%(asctime)s %(name)-7s %(levelname)-7s %(message)s')
            logfile_hdler.setFormatter(formatter)
            root.addHandler(logfile_hdler)
            return logfile_hdler

    return None

def networks_option(networks, parser):
    """Return a list of ipaddress objects from a comma separated list.

    NETWORKS is a comma separated list of local IPv4 interfaces or local IPv4
    addresses where UPnP devices may be discovered. An IPv4 interface is
    written using the "IP address/network prefix" slash notation (aka CIDR
    notation) as printed by the 'ip address' linux command.
    When this option is an empty string or the option is missing, all the
    interfaces are used, except the 127.0.0.1/8 loopback interface.
    """

    network_objects = []
    if networks:
        for network in (x.strip() for x in networks.split(',')):
            try:
                if '/' in network:
                    obj = ipaddress.IPv4Interface(network)
                    if obj.network.prefixlen == 32:
                        parser.error(
                            f'{network} not a valid network interface')
                        continue
                else:
                    obj = ipaddress.IPv4Address(network)
            except ValueError as e:
                parser.error(e)
            network_objects.append(obj)

    else:
        # Use the ip command to get the list of the IPv4 interfaces.
        cmd = 'ip -family inet -brief -json address show'
        try:
            proc = subprocess.run(cmd.split(),
              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError as e:
            parser.error("'ip' command not available, please use the"
                         ' --networks option')
        try:
            json_out = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            parser.error(f'json loads exception in {proc.stdout}: {e}')

        logger.debug(f'Output of "{cmd}"\n:{json_out}')
        for item in json_out:
            for addr in item['addr_info']:
                ip = addr['local']
                prefixlen = addr['prefixlen']
                if ip != '127.0.0.1' and prefixlen != 32:
                    iface = ipaddress.IPv4Interface((ip, prefixlen))
                    network_objects.append(iface)

    if not network_objects:
        parser.error('no network interface available')

    return network_objects

def parse_args(doc, pa_dlna):
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
    parser.add_argument('--networks', '-n', default='',
                        help=' '.join(line.strip() for line in
                                     networks_option.__doc__.split('\n')[2:]))
    parser.add_argument('--port', type=int, default=8080,
                        help='set the TCP port on which the HTTP server'
                        ' handles DLNA requests (default: %(default)s)')
    parser.add_argument('--ttl', type=pack_B, default=b'\x02',
                        help='set the IP packets time to live to TTL'
                        ' (default: 2)')
    if pa_dlna:
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
    options = vars(parser.parse_args())

    dump_default = options.get('dump_default')
    dump_internal = options.get('dump_internal')
    if dump_default and dump_internal:
        parser.error(f"Cannot set both '--dump-default' and "
                     f"'--dump-internal' arguments simultaneously")
    if dump_default or dump_internal:
        return options, None

    logfile_hdler = setup_logging(options)

    # Run networks_option() once logging has been setup.
    logger.info('Python version ' + sys.version)
    logger.info(f'Options {options}')
    options['networks'] = networks_option(options['networks'], parser)
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
def padlna_main(clazz, doc):

    def run_in_thread(coro):
        """Run the UPnP control point in a thread."""

        cp_thread = threading.Thread(target=asyncio.run, args=[coro])
        cp_thread.start()
        return cp_thread

    assert clazz.__name__ in ('AVControlPoint', 'UPnPControlCmd')
    pa_dlna = True if clazz.__name__ == 'AVControlPoint' else False

    # Parse the arguments.
    options, logfile_hdler = parse_args(doc, pa_dlna)

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
            logger.exception(f'{e!r}')
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
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt as e:
        logger.info(f'{app} got {e!r}')
    finally:
        logger.info(f'End of {app}')
        if logfile_hdler is not None:
            logfile_hdler.flush()
        logging.shutdown()
        sys.exit(exit_code)
