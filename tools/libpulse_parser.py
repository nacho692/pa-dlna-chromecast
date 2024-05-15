""" Preprocess and parse the libpulse headers.

    Function signatures are found when parsing:
        - 'types' (callbacks).
        - 'structs' (arrays of function pointers).
        - 'functions'.
        - as argument of a function when this is a function pointer itself,
          for example in 'pa_mainloop_api_once'.
"""

import os
import sys
import textwrap
import shutil
import subprocess
import pprint

from pyclibrary.c_library import CParser

PULSEAUDIO_H = '/usr/include/pulse/pulseaudio.h'

class ParseError(Exception): pass

def dedent(txt):
    """A dedent that does not use the first line to compute the margin."""

    lines = txt.splitlines(keepends=True)
    # Find the first non empty line, skipping the first line.
    idx = 1
    for i, l in enumerate(lines[1:]):
        if l != '\n':
            idx = i + 1
            break
    return ''.join(lines[:idx]) + textwrap.dedent(''.join(lines[idx:]))

def preprocess(header, pathname):
    with open(pathname, 'w') as f:
        proc = subprocess.run(['gcc', '-E', '-P', header],
                              stdout=f, text=True)
    print(f"'{pathname}' created")

def get_parser():
    if shutil.which('gcc') is None:
        print('*** Error: GNU gcc is required', file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(PULSEAUDIO_H):
        print(f'*** Error: {PULSEAUDIO_H} does not exist', file=sys.stderr)
        sys.exit(1)

    dirname = os.path.dirname(__file__)
    pulse_cpp = os.path.join(dirname, 'libpulse.cpp')
    pulse_cache = os.path.join(dirname, 'libpulse.cache')

    if not os.path.exists(pulse_cpp):
        preprocess(PULSEAUDIO_H, pulse_cpp)

    cache_built = False
    if not os.path.exists(pulse_cache):
        print(f"Building PyCLibrary cache file '{pulse_cache}'.")
        print('Please wait, this may take a while ...')
        cache_built = True

    parser = CParser([pulse_cpp], cache=pulse_cache)
    if cache_built:
        print(f"'{pulse_cache}' created")

    return parser

def lib_generator(parser, type):
    return ((name, item) for (name, item) in parser.defs[type].items() if
            name.startswith('pa_'))

def signature_index(type_instance):
    # A Type instance is a function signature when one of its members is a
    # tuple.
    sig_idx = 0
    for i, item in enumerate(type_instance):
        if i == 0:
            # The type name.
            continue
        if isinstance(item, tuple):
            sig_idx = i
            break
    return sig_idx

def get_type(type_instance):
    items = []
    for item in type_instance:
        # An array.
        if (isinstance(item, list) and len(item) == 1 and
                isinstance(item[0], int)):
            count = int(item[0])
            if count > 0:
                item = f'* {count}'
            else:
                item = '*'
        items.append(item)
    return ' '.join(items)

def parse_types(parser):
    types = {}
    callbacks = {}
    for name, type_instance in lib_generator(parser, 'types'):
        if signature_index(type_instance):
            # Signature of a function pointer.
            callbacks[name] = (type_instance[0], list(get_type(s[1]) for s in
                                                  type_instance[1]))
        else:
            types[name] = type_instance[0]
    return types, callbacks

def parse_enums(parser):
    enums = {}
    for name, enum in lib_generator(parser, 'enums'):
        enums[name] = enum
    return enums

def parse_array(struct):
    array = {}
    for member in struct.members:
        name = member[0]
        type_instance = member[1]
        sig_idx = signature_index(type_instance)
        if sig_idx:
            # A structure member as a function pointer.
            # The signature has a variable length as the return type is not a
            # Type instance as usual but a variable number of str elements.
            restype = ' '.join(type_instance[:sig_idx])
            arg_types = list(get_type(s[1]) for s in type_instance[sig_idx])
            array[name] = (restype, arg_types)

        else:
            # A structure member as a plain type.
            array[name] = get_type(type_instance)
            continue

    return array

def parse_structs(parser):
    structs ={}
    arrays = {}
    try:
        for name, struct in lib_generator(parser, 'structs'):
            # An array of function pointers.
            if name in ('pa_mainloop_api', 'pa_spawn_api'):
                arrays[name] = parse_array(struct)
                continue

            result = []
            for member in struct.members:
                result.append((member[0], get_type(member[1])))
            structs[name] = tuple(result)

    except Exception as e:
        raise ParseError(f"Structure '{name}': {struct}") from e

    return structs, arrays

def parse_functions(parser):
    functions = {}
    for name, func in lib_generator(parser, 'functions'):
        assert signature_index(func)
        try:
            restype = " ".join(func[0])
            arg_types = []
            for arg in func[1]:
                type_instance = arg[1]

                if signature_index(type_instance):
                    # Signature of a function pointer.
                    arg_types.append((type_instance[0],
                            list(get_type(s[1]) for s in type_instance[1])))
                else:
                    arg_types.append(get_type(type_instance))

            functions[name] = (restype, arg_types)

        except Exception as e:
            raise ParseError(f"Function '{name}': {func}") from e

    return functions

def main():
    parser = get_parser()

    types, callbacks = parse_types(parser)
    enums = parse_enums(parser)
    structs, arrays = parse_structs(parser)
    signatures = parse_functions(parser)

    # The files of the parsed sections are written to 'dirname'.
    dirname = '.'
    if len(sys.argv) > 1:
        dirname = sys.argv[1]
        if not os.path.isdir(dirname):
            print(f"*** Error: '{dirname}' is not a directory",
                  file=sys.stderr)
            sys.exit(1)

    # Merge all signatures into a 'functions' dictionary.
    functions = {}
    functions['signatures'] = {}
    functions['signatures'].update(signatures)
    functions['callbacks'] = callbacks
    for name, signature in arrays['pa_mainloop_api'].items():
        if name != 'userdata':
            functions['callbacks'][name] = signature

    # Create the parsed sections files.
    for name in ('types', 'enums', 'structs', 'functions'):
        pulse_name = 'pulse_' + name
        doc = f'''"""File {pulse_name}.

        This file has been generated by libpulse_parser.py - DO NOT MODIFY.
        """

        '''

        pathname = os.path.join(dirname, pulse_name + '.py')
        with open(pathname, 'w') as f:
            print(dedent(doc), file=f)
            print(f'{pulse_name} = ', file=f, end='\\\n')
            pprint.pprint(eval(name), stream=f)
            print(f"'{pathname}' created")

if __name__ == '__main__':
    main()
