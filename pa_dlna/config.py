"""The default and the user configurations."""

import sys
import os
import textwrap
import logging
from configparser import ConfigParser, ParsingError

from . import pprint_pformat
from . import encoders as encoders_module

logger = logging.getLogger('config')
BOOLEAN_WRITE = {'True': 'yes', 'False': 'no'}
BOOLEAN_PARSE = {'yes': True, 'no': False}

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

class DefaultConfig(Config):
    """The default built-in configuration."""

    def __init__(self):
        super().__init__()
        self.encoder_list = []
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

    def override_options(self, encoder, section, defaults):
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
                self.override_options(encoder, section, defaults)

                # Python 3.7: Dictionary order is guaranteed to be
                # insertion order.
                self[section] = encoder
                self.encoder_list.append(section)
            else:
                raise ParsingError(f"'{section}' not a valid class name")

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

        # First clear the dictionary built by DefaultConfig as it does not
        # reflect anymore the current state of the parser.
        self.clear()
        self.build_dictionary()

    def build_dictionary(self):
        super().build_dictionary()

        # Update the dict with the [EncoderName.UDN] sections.
        defaults = self.parser.defaults()
        for section in self.parser:
            encoder_name, sep, udn = section.partition('.')
            # Ignore an encoder section.
            if sep == '' and udn == '':
                continue
            # Error when section is 'encoder_name.'
            elif udn == '':
                raise ParsingError(f"'{section}' not a valid section")

            if encoder_name not in self.encoder_list:
                raise ParsingError(f"'{section}' encoder does not exist")

            encoder = self.leaves[encoder_name]()
            self.override_options(encoder, section, defaults)
            self[udn] = encoder

    def print_internal_config(self):
        udns = {}
        encoders = {}
        for section, encoder in self.items():
            if hasattr(encoder, 'selection'):
                del encoder.selection

            # An udn section.
            if section not in self.encoder_list:
                options = {'_encoder': encoder.__class__.__name__}
                options.update(encoder.__dict__)
                udns[section] = options
            # An encoder section.
            else:
                encoders[section] = encoder.__dict__

        # The udn sections are printed first.
        udns.update(encoders)
        encoders_repr = pprint_pformat(udns, sort_dicts=False, compact=True)
        sys.stdout.write('Internal configuration, ')
        sys.stdout.write('keys starting with underscore are read only:\n')
        sys.stdout.write(f'{encoders_repr}\n')
