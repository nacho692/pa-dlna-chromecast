"""Generate the default-config file in the documentation."""

import sys
from subprocess import Popen, PIPE

DEFAULT_CONFIG = 'docs/source/default-config.rst'
HEADER = """\
.. _default_config:

built-in default configuration
==============================

::

"""

def main():
    try:
        with open(DEFAULT_CONFIG, 'w') as f:
            f.write(HEADER)
            with Popen([sys.executable, '-m', 'pa_dlna.pa_dlna',
                        '--dump-default'], stdout=PIPE) as proc:
                for line in proc.stdout:
                    line = line.decode()
                    line = line.replace('\t', '    ').rstrip()
                    if line:
                        f.write(' ' + line + '\n')
                    else:
                        f.write('\n')
    except FileNotFoundError as e:
        print(f'{e!r}: {DEFAULT_CONFIG}',
              file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
