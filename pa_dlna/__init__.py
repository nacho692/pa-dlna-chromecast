"""Utilities for starting an UPnPApplication."""

import sys
import os
import argparse
import ipaddress
import subprocess
import json
import logging
import pprint
import functools
import textwrap
import asyncio
import threading
from configparser import ConfigParser, ParsingError

from . import encoders as encoders_module

logger = logging.getLogger('init')
BOOLEAN_PARSE = {'yes': True, 'no': False}
BOOLEAN_WRITE = {'True': 'yes', 'False': 'no'}

__version__ = '0.1'
MIN_PYTHON_VERSION = (3, 8)

VERSION = sys.version_info
if VERSION[0] != MIN_PYTHON_VERSION[0] or VERSION < MIN_PYTHON_VERSION:
    print(f'error: the python version must be at least'
          f' {MIN_PYTHON_VERSION}', file=sys.stderr)
    sys.exit(1)

# We want to preserve the order of 'in' and 'out' elements in the 'actionList'
# of the service xml description.
# The 'sort_dicts' keyword is supported since 3.8.
if VERSION >= (3, 8):
    pprint_pprint = functools.partial(pprint.pprint, sort_dicts=False)
    pprint_pformat = functools.partial(pprint.pformat, sort_dicts=False)
else:
    pprint_pprint = pprint.pprint
    pprint_pformat = pprint.pformat

# Encoders configuration.
def new_cfg_parser(**kwargs):
    # 'allow_no_value' to write comments as fake options.
    parser = ConfigParser(allow_no_value=True, **kwargs)
    # Do not convert option names to lower case in interpolations.
    parser.optionxform = str
    parser.BOOLEAN_STATES = BOOLEAN_PARSE
    return parser

def comments_from_doc(doc):
    """A generator of comments from text."""

    lines = doc.splitlines()
    doc = lines[0] + '\n' + textwrap.dedent('\n'.join(l for l in lines[1:]
                                                if l == '' or l.strip()))
    for line in doc.splitlines():
        yield '# ' + line if line else '#'

def user_config_pathname():
    base_path = os.environ.get('XDG_CONFIG_HOME')
    if base_path is None:
        base_path = os.path.expanduser('~/.config')
    return os.path.join(base_path, 'pa-dlna', 'pa-dlna.conf')

class Config(dict):
    def __init__(self):
        self.root_class = encoders_module.ROOT_ENCODER
        self.parser = None

        # Build a dictionary of the leaves of the 'root_class'
        # class hierarchy excluding the direct subclasses.
        m = encoders_module
        self.leaves = dict((name, obj) for
                            (name, obj) in m.__dict__.items() if
                                isinstance(obj, type) and
                                issubclass(obj, self.root_class) and
                                obj.__mro__.index(self.root_class) != 1 and
                                not obj.__subclasses__())

    def get_value(self, section, encoder, option, new_val):
        old_val = getattr(encoder, option)
        if old_val is not True and old_val is not False:
            for t in (int, float):
                if isinstance(old_val, t):
                    try:
                        return t(new_val)
                    except ValueError as e:
                        raise ParsingError(f'{section}.{option}: {e}')
        try:
            return self.parser.getboolean(section, option)
        except ValueError:
            pass
        return new_val

    def build_dictionary(self):
        """Build this 'dict' as an image of the parser.

        Config is a subclass of 'dict'.
        """

        if self.parser is None:
            return

        defaults = self.parser.defaults()
        selection = (s.strip() for s in defaults['selection'].split(','))
        for section in (s for s in selection if s):
            if section in self.leaves:
                encoder = self.leaves[section]()
                for option, value in self.parser.items(section):
                    if option.startswith('#'):
                        continue
                    if (hasattr(encoder, option) and
                            not option.startswith('_')):
                        new_val = self.get_value(section, encoder,
                                                 option, value)
                        if new_val is not None:
                            setattr(encoder, option, new_val)
                    elif option not in defaults:
                        raise ParsingError(f'Unknown option'
                                           f" '{section}.{option}'")
                # Python 3.7: Dictionary order is guaranteed to be
                # insertion order.
                self[section] = encoder
            else:
                raise ParsingError(f"'{section}' not a valid class name")

class DefaultConfig(Config):
    """The default built-in configuration."""

    def __init__(self):
        super().__init__()
        self.empty_comment_cnt = 0
        self.default_config()
        self.build_dictionary()

    def write_empty_comment(self, section):
        # Make ConfigParser believe that we are adding each time
        # a different option with no value.
        self.parser.set(section, "#" + self.empty_comment_cnt * ' ')
        self.empty_comment_cnt += 1

    def default_config(self):
        """Build a parser holding the built-in default configuration."""

        def convert_boolean(obj, attr):
            val = str(getattr(obj, attr))
            if val in BOOLEAN_WRITE:
                val = BOOLEAN_WRITE[val]
            return val

        root = self.root_class()
        sections = root.selection
        defaults = {'selection': '\n' + ',\n'.join(sections) + ','}
        for attr in root.__dict__:
            if attr != 'selection' and not attr.startswith('_'):
                val = convert_boolean(root, attr)
                defaults[attr] = val
        self.parser = new_cfg_parser(defaults=defaults)

        for section in sorted(sections):
            if section not in self.leaves:
                raise ParsingError(f"'{section}' is not a valid class name")
            self.parser.add_section(section)
            encoder = self.leaves[section]()
            doc = encoder.__class__.__doc__
            if doc:
                for comment in comments_from_doc(doc):
                    if comment == '#':
                        self.write_empty_comment(section)
                    else:
                        self.parser.set(section, comment)
                self.write_empty_comment(section)

            write_separator = True
            for attr in encoder.__dict__:
                val = convert_boolean(encoder, attr)
                if attr.startswith('_'):
                    self.parser.set(section, f'# {attr[1:]}: {val}')
                elif (not hasattr(root, attr) or
                      getattr(root, attr) != getattr(encoder, attr)):
                    if write_separator:
                        write_separator = False
                        self.write_empty_comment(section)
                    self.parser.set(section, attr, val)

    def write(self, fileobject):
        """Write the configuration to a text file object."""

        for comment in comments_from_doc(self.root_class.__doc__):
            fileobject.write(comment + '\n')
        fileobject.write('\n')

        if self.parser is not None:
            self.parser.write(fileobject)

class UserConfig(DefaultConfig):
    """The user configuration.

    The configuration derived from the 'pa-dlna.conf' file and the
    default configuration. Only the encoders selected by the user are listed.
    """

    def __init__(self):
        super().__init__()

        # Read the user configuration.
        user_config = user_config_pathname()
        try:
            fileobject = open(user_config)
        except FileNotFoundError:
            return
        else:
            with fileobject:
                logger.info(f'Using encoders configuration at {user_config}')
                self.parser.read_file(fileobject)

        # First clear the dictionary built by DefaultConfig.
        self.clear()
        self.build_dictionary()

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

    if options['nolog_upnp']:
        logging.getLogger('upnp').addFilter(FilterDebug())
        logging.getLogger('network').addFilter(FilterDebug())
    if not options['log_aio']:
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

def networks_option(ip_interfaces, parser):
    """Return a list of ipaddress.IPv4Interface from a comma separated list.

    IP_INTERFACES is a comma separated list of the IPv4 interfaces where UPnP
    devices may be discovered. An IP_INTERFACE is written using the
    "network address/network prefix" notation as printed by the
    'ip address list' command.
    When this option is an empty string or the option is missing, all the
    interfaces are used, except the 127.0.0.1/8 loopback interface.
    """

    net_ifaces = []
    if ip_interfaces:
        for ip_interface in (x.strip() for x in ip_interfaces.split(',')):
            try:
                iface = ipaddress.IPv4Interface(ip_interface)
            except ValueError as e:
                parser.error(e)
            if iface.network.prefixlen == 32:
                logger.warning(f'{ip_interface} network prefix length is 32')
            net_ifaces.append(iface)

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
                if ip != '127.0.0.1':
                    iface = ipaddress.IPv4Interface((ip, prefixlen))
                    net_ifaces.append(iface)

        if not net_ifaces:
            parser.error('no network interface available')

    return net_ifaces

def parse_args(doc, loglevel_default):
    """Parse the command line."""

    parser = argparse.ArgumentParser(description=doc)
    parser.add_argument('--version', '-v', action='version',
                        version='%(prog)s: version ' + __version__)
    parser.add_argument('--networks', '-n', metavar="IP_INTERFACES",
                        default='', dest='ip_interfaces',
                        help=' '.join(line.strip() for line in
                                     networks_option.__doc__.split('\n')[2:]))
    parser.add_argument('--port', type=int, default=8080,
                        help='set the TCP port on which the HTTP server'
                        ' handles DLNA requests (default: %(default)s)')
    parser.add_argument('--ttl', type=int, default=2,
                        help='set the IP packets time to live to TTL'
                        ' (default: %(default)s)')
    parser.add_argument('--renderers', '-r', metavar='MIME-TYPES',
                        default='', dest='renderers_mtypes',
                        help='MIME-TYPES is a comma separated list of audio '
                        'mime types - a TestRenderer is instantiated for'
                        ' each of these mime types and a pulseaudio stream '
                        'may be run by doing an http GET on the '
                        'TestRenderer url provided by the logs, the '
                        'stream is routed to the TestRenderer and '
                        'collected by the program doing the http GET (curl'
                        ' for example)')
    parser.add_argument('--encoders-default', '-d', action='store_true',
                        help='write to stdout (and exit) the default encoders'
                        ' configuration')
    parser.add_argument('--loglevel', '-l', default=loglevel_default,
                        choices=('debug', 'info', 'warning', 'error'),
                        help='set the log level of the stderr logging console'
                        ' (default: %(default)s)')
    parser.add_argument('--logfile', '-f', metavar='PATH',
                        help='add a file logging handler set at '
                        "'debug' log level whose path name is PATH")
    parser.add_argument('--nolog-upnp', '-u', action='store_true',
                        help="ignore upnp log entries at 'debug' log level")
    parser.add_argument('--log-aio', '-a', action='store_true',
                        help='do not ignore asyncio log entries at'
                        " 'debug' log level; the default is to ignore those"
                        ' verbose logs')
    parser.add_argument('--config-internal', '-i', action='store_true',
                        help='write to stdout (and exit) the configuration'
                        '  used internally by the program on startup - mostly'
                        ' for debugging')

    # Options as a dict.
    options = vars(parser.parse_args())

    if options['encoders_default'] and options['config_internal']:
        parser.error(f"Cannot set both '--encoder-default' and "
                     f"'--encoder-internal' arguments simultaneously")
    if options['encoders_default'] or options['config_internal']:
        return options, None

    logfile_hdler = setup_logging(options)

    # Run networks_option() once logging has been setup.
    logger.info(f'Options {options}')
    options['net_ifaces'] = networks_option(options['ip_interfaces'], parser)

    return options, logfile_hdler

# Class.
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
def main_function(clazz, doc, loglevel_default='info', inthread=False):

    def run_in_thread(coro):
        """Run the UPnP control point in a thread."""

        cp_thread = threading.Thread(target=asyncio.run, args=[coro])
        cp_thread.start()
        return cp_thread

    assert issubclass(clazz, UPnPApplication)

    # Parse the arguments.
    options, logfile_hdler = parse_args(doc, loglevel_default)

    # Get the encoders configuration.
    try:
        if options['encoders_default']:
            DefaultConfig().write(sys.stdout)
            sys.exit(0)

        config = UserConfig()
        if options['config_internal']:
            _encoders = {}
            for name, encoder in config.items():
                if hasattr(encoder, 'selection'):
                    del encoder.selection
                _encoders[name] = encoder.__dict__
            encoders_repr = pprint_pformat(_encoders, sort_dicts=False,
                                         compact=True)
            sys.stdout.write(f'Encoders configuration:\n{encoders_repr}\n')
            sys.exit(0)
    except Exception as e:
        logger.exception(f'{e!r}')
        sys.exit(1)

    # Run the UPnPApplication instance.
    app = clazz(config=config, **options)
    logger.info(f'Start {app}')
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
