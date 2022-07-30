"""Utilities for starting an UPnPApplication."""

import sys
import os
import argparse
import ipaddress
import subprocess
import json
import logging
import pprint
import textwrap
import asyncio
import threading
from configparser import ConfigParser, ParsingError

logger = logging.getLogger('init')

__version__ = '0.1'
MIN_PYTHON_VERSION = (3, 7)

ver = sys.version_info
if ver[0] != MIN_PYTHON_VERSION[0] or ver < MIN_PYTHON_VERSION:
    print(f'error: the python version must be at least'
          f' {MIN_PYTHON_VERSION}', file=sys.stderr)
    sys.exit(1)

# Encoders configuration.
try:
    from . import encoders

    def new_cfg_parser(**kwargs):
        # 'allow_no_value' to write comments as fake options.
        parser = ConfigParser(allow_no_value=True, **kwargs)
        # Do not convert option names to lower case in interpolations.
        parser.optionxform = str
        return parser

    def comments_from_doc(doc):
        """A generator of comments from text."""

        lines = doc.splitlines()
        doc = lines[0] + '\n' + textwrap.dedent('\n'.join(l for l in lines[1:]
                                                    if l == '' or l.strip()))
        for line in doc.splitlines():
            yield '# ' + line if line else '#'

    def codecs_config(codecs_path):
        """Set up encoders configuration."""

        # Write the encoders default configuration when the 'codecs_path'
        # argument has been set and exit.
        if codecs_path is not None:
            default_config = EncodersConfig()
            with open(codecs_path, 'w') as f:
                default_config.write(f)
                logger.info(
                    f"Writing encoders configuration file '{codecs_path}'")
                sys.exit(0)

        # Try to load the user configuration file, otherwise use the default
        # configuration.
        xdg_config_home = os.environ.get('XDG_CONFIG_HOME')
        if xdg_config_home is None:
            xdg_config_home = os.path.expanduser('~/.config')
        codecs_path = os.path.join(xdg_config_home, 'pa-dlna', 'pa-dlna.ini')

        # Read the user configuration.
        try:
            with open(codecs_path) as f:
                logger.info(f'Using encoders configuration at {codecs_path}')
                config = EncodersConfig(f)
        except OSError:
            config = EncodersConfig()

        return config

    class EncodersConfig(dict):
        """A mapping of encoders class names to their attributes.

        The mapping is backed by a ConfigParser instance that may be read from
        (or may be written to) an '.INI' configuration file.
        The priority of the configuration is given by the order in which the
        sections (class names) are listed, the first being the highest
        priority.
        """

        def __init__(self, fileobject=None,
                     root_class=encoders.ROOT_ENCODER):
            self.root_class = root_class
            self.empty_comment_cnt = 0

            # Build a dictionary of the leaves of the 'root_class'
            # class hierarchy excluding the direct subclasses.
            m = sys.modules[root_class.__module__]
            self.leaves = dict((name, obj) for
                                (name, obj) in m.__dict__.items() if
                                    isinstance(obj, type) and
                                    issubclass(obj, root_class) and
                                    obj.__mro__.index(root_class) != 1 and
                                    not obj.__subclasses__())

            if fileobject is not None:
                self.parser = self.read(fileobject)
            else:
                self.parser = self.default_config()
            self.build_dictionary()

        def write_empty_comment(self, parser, section):
            # Make ConfigParser believe that we are adding each time
            # a different option with no value.
            parser.set(section, "#" + self.empty_comment_cnt * ' ')
            self.empty_comment_cnt += 1

        def default_config(self, classes=encoders.DEFAULT_CONFIG):

            parser = new_cfg_parser(defaults={'selection':
                                            '\n' + ',\n'.join(classes) + ','})
            for n in sorted(classes):
                if n not in self.leaves:
                    raise ParsingError(f"'{n}' is not a valid class name")
                parser.add_section(n)
                instance = self.leaves[n]()
                doc = instance.__class__.__doc__
                if doc:
                    for comment in comments_from_doc(doc):
                        if comment == '#':
                            self.write_empty_comment(parser, n)
                        else:
                            parser.set(n, comment)
                    self.write_empty_comment(parser, n)

                write_separator = True
                for attr in instance.__dict__:
                    if attr.startswith('_'):
                        parser.set(n,
                                   f'# {attr[1:]}: {getattr(instance, attr)}')
                    else:
                        if write_separator:
                            write_separator = False
                            self.write_empty_comment(parser, n)
                        parser.set(n, attr, str(getattr(instance, attr)))
            return parser

        def get_value(self, section, instance, option, new_val):
            old_val = getattr(instance, option)
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
            if self.parser is None:
                return

            defaults = self.parser.defaults()
            selection = (s.strip() for s in defaults['selection'].split(','))
            for section in (s for s in selection if s):
                if section in self.leaves:
                    instance = self.leaves[section]()
                    for option, value in self.parser.items(section):
                        if option.startswith('#'):
                            continue
                        if (hasattr(instance, option) and
                                not option.startswith('_')):
                            new_val = self.get_value(section, instance,
                                                     option, value)
                            if new_val is not None:
                                setattr(instance, option, new_val)
                        elif option not in defaults:
                            raise ParsingError(f'Unknown option'
                                               f" '{section}.{option}'")
                    # Python 3.7: Dictionary order is guaranteed to be
                    # insertion order.
                    self[section] = instance.__dict__
                else:
                    raise ParsingError(f"'{section}' not a valid class name")

        def read(self, fileobject):
            """Read and parse a configuration file."""

            parser = new_cfg_parser()
            parser.read_file(fileobject)
            return parser

        def write(self, fileobject):
            """Write the configuration to a text file object."""

            for comment in comments_from_doc(self.root_class.__doc__):
                fileobject.write(comment + '\n')
            fileobject.write('\n')

            if self.parser is not None:
                self.parser.write(fileobject)

except ImportError:
    def codecs_config(codecs_path):
        return None

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

def networks_option(ip_list, parser):
    """Return a list of IPv4 addresses from a comma separated list.

    IP_LIST is a comma separated list of the local IPv4 addresses of the
    network interfaces where UpnP devices may be discovered.
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

def parse_args(doc):
    """Parse the command line."""

    parser = argparse.ArgumentParser(description=doc)
    parser.add_argument('--version', '-v', action='version',
                        version='%(prog)s: version ' + __version__)
    parser.add_argument('--networks', '-n', metavar="IP_LIST", default='',
                        dest='ip_list',
                        help=' '.join(line.strip() for line in
                                     networks_option.__doc__.split('\n')[2:]))
    parser.add_argument('--ttl', type=int, default=2,
                        help='set the IP packets time to live to TTL'
                        ' (default: %(default)s)')
    parser.add_argument('--write-encoders', '-w', metavar='PATH',
                        dest='encoders_path',
                        help='write the default encoders configuration to'
                        ' PATH and exit')
    parser.add_argument('--loglevel', '-l', default='error',
                        choices=('debug', 'info', 'warning', 'error'),
                        help='set the log level of the stderr logging console'
                        ' (default: %(default)s)')
    parser.add_argument('--logfile', '-f', metavar='PATH',
                        help='add a PATH logging file handler set at '
                        "'debug' log level")
    parser.add_argument('--logaio', '-a', action='store_true',
                        help='do not ignore asyncio log entries at'
                        " 'debug' log level; the default is to ignore those"
                        ' verbose logs')

    # Options as a dict.
    options = vars(parser.parse_args())

    logfile_hdler = setup_logging(options)

    # Run networks_option() once logging has been setup.
    options['ip_list'] = networks_option(options['ip_list'], parser)
    logger.info(f'Options {options}')

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
def main_function(clazz, doc, inthread=False):

    def run_in_thread(coro):
        """Run the UPnP control point in a thread."""

        cp_thread = threading.Thread(target=asyncio.run, args=[coro])
        cp_thread.start()
        return cp_thread

    assert issubclass(clazz, UPnPApplication)

    # Parse the arguments.
    options, logfile_hdler = parse_args(doc)

    try:
        codecs = codecs_config(options['encoders_path'])
        if codecs is not None:
            codecs_repr = pprint.pformat(codecs, sort_dicts=False,
                                         compact=True)
            logger.debug(f'Encoders configuration:\n{codecs_repr}')
    except Exception as e:
        sys.exit(repr(e))

    # Run the UPnPApplication instance.
    app = clazz(encoders=codecs, **options)
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
