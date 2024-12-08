"""Update the version using 'git describe'."""

import sys
import re
import subprocess
from packaging import version as pkg_version

INIT_FILE = 'pa_dlna/__init__.py'

def normalize(version, do_print=False):
    "Check and normalize 'version' as conform to PEP 440."

    try:
        v = pkg_version.Version(version)
        if do_print:
            v_dict = {
                'release': v.release,
                'pre': v.pre,
                'post': v.post,
                'dev': v.dev,
                'local': v.local,
            }
            for name, value in v_dict.items():
                print(f'{name}: {value}', file=sys.stderr)
        return v
    except pkg_version.InvalidVersion as e:
        print(e, file=sys.stderr)
        sys.exit(1)

def main():
    """Set the development version name in INIT_FILE.

    The 'git describe' command outputs:
        release'-'number_of_commits_since_last_tag'-g'short_commit_sha

    After all the characters after the last '-' in the output have been
    striped, the version is normalized (made conform to PEP 440) as:
        release'.post'number_of_commits_since_last_tag

    For example '0.14.post3'.
    """

    version = subprocess.check_output(['git', 'describe'])
    version = version.decode().strip()
    version = normalize(version.rsplit('-', maxsplit=1)[0], do_print=True)
    if version.post is None:
        print(f'*** Error:\n  Cannot set a development version name at release'
              f' {version}.\n  It must be followed by at least one commit.',
              file=sys.stderr)
        sys.exit(1)

    with open(INIT_FILE) as f:
        txt = f.read()

    regexp = re.compile(r"(__version__\s+=\s+)'([^']+)'")
    new_txt = regexp.sub(rf"\1'{version}'", txt)

    with open(INIT_FILE, 'w') as f:
        f.write(new_txt)
    print(f"{INIT_FILE} has been updated with: __version__ = '{version}'",
          file=sys.stderr)

if __name__ == '__main__':
    main()
