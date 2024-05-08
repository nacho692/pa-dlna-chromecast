""" Preprocess and parse the libpulse headers.

    Function signatures are found when parsing:
        - 'types' (callbacks).
        - 'structs' (arrays of function pointers).
        - 'functions'.
        - as argument of a function when this is a function pointer itself,
          for example in 'pa_mainloop_api_once'.
"""

import os
import subprocess
import pprint

from pyclibrary.c_library import CLibrary

PULSEAUDIO_H = '/usr/include/pulse/pulseaudio.h'
PULSE_CPP = './libpulse.cpp'
PULSE_CACHE = './libpulse.cache'

class ParseError(Exception): pass

def preprocess(header, pathname):
    with open(pathname, 'w') as f:
        proc = subprocess.run(['gcc', '-E', '-P', header],
                              stdout=f, text=True)

def lib_generator(parser, type):
    return ((name, item) for (name, item) in parser.defs[type].items() if
            name.startswith('pa_') or name.startswith('PA_'))

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

def parse_all():
    if not os.path.exists(PULSE_CPP):
        preprocess(PULSEAUDIO_H, PULSE_CPP)

    if not os.path.exists(PULSE_CACHE):
        print(f"Building PyCLibrary cache file '{PULSE_CACHE}'.")
        print('Please wait, this may take a while ...')
    clib = CLibrary('pulse', [PULSE_CPP], cache=PULSE_CACHE)

    parser = clib._headers_
    types, callbacks = parse_types(parser)
    enums = parse_enums(parser)
    structs, arrays = parse_structs(parser)
    functions = parse_functions(parser)

    sections = {
        'types':            types,
        'enums':            enums,
        'structs':          structs,
        'functions':        functions,
        'callbacks':        callbacks,
        'pa_mainloop_api':  arrays['pa_mainloop_api'],
        'pa_spawn_api':     arrays['pa_spawn_api'],
        }
    return clib, sections

def main():
    clib, sections = parse_all()

    for name in sections:
        print(f'# ===================== {name} =====================')
        pprint.pprint(sections[name])

if __name__ == '__main__':
    main()
