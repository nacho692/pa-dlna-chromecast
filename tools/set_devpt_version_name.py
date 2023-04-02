"""Update the version using 'git describe'."""

import sys
import re
import subprocess

INIT_FILE = 'pa_dlna/__init__.py'

def main():
    regexp = re.compile(r"(__version__\s+=\s+)'([^']+)'")
    version = subprocess.check_output(['git', 'describe'])
    version = version.decode().strip()

    with open(INIT_FILE) as f:
        txt = f.read()

    new_txt = regexp.sub(rf"\1'{version}'", txt)

    if new_txt != txt:
        with open(INIT_FILE, 'w') as f:
            f.write(new_txt)
        print(f"{INIT_FILE} has been updated with __version__ = '{version}'",
              file=sys.stderr)
    else:
        print(f'*** Error: fail to update {INIT_FILE}', file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
