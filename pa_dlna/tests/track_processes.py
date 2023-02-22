"""The parec (or encoder) script used for testing."""

import sys
import os
import io
import socket
import time
import tempfile
import contextlib
from unittest import mock

BLKSIZE = 2 ** 12   # 4096
ADEN_ARABIE = (b"J'avais vingt ans. Je ne laisserai personne dire que c'est"
               b' le plus bel age de la vie.')
PAREC_PATH_ENV = 'PA_DLNA_PAREC_PATH'
ENCODER_PATH_ENV = 'PA_DLNA_ENCODER_PATH'
STDIN_FILENO = 0
STDOUT_FILENO = 1

def get_blk():
    """Return BLKSIZE bytes."""

    hunk = ADEN_ARABIE * (BLKSIZE // len(ADEN_ARABIE) + 1)
    hunk = hunk[:BLKSIZE]
    assert len(hunk) == BLKSIZE
    return hunk

@contextlib.contextmanager
def unix_socket_path(socket_path_env):
    path = tempfile.mktemp(prefix="test_http_", suffix='.sock',
                           dir=os.path.curdir)
    path = os.path.abspath(path)
    with mock.patch.dict('os.environ', {socket_path_env: path}):
        yield path
        try:
            os.unlink(path)
        except OSError:
            pass

def handle_first_command(sock):
    return_code = 0
    do_sleep = True
    resp = b'Ok'
    exception = None

    command = sock.recv(1024)
    command = command.decode()

    if command == 'ignore':
        pass
    elif command == 'dont_sleep':
        do_sleep = False
    elif command == 'FFMpegEncoder':
        return_code = 255
        do_sleep = False
    else:
        try:
            obj = eval(command + '()')
        except NameError:
            resp = b'NameError'
        else:
            if isinstance(obj, Exception):
                exception = obj
            else:
                resp = b'Unknown'
    sock.sendall(resp)

    if exception is not None:
        raise exception

    return return_code, do_sleep

def parec():
    print('parec stub starting', file=sys.stderr)
    return_code = 0
    hunk = get_blk()
    stdout = io.BufferedWriter(io.FileIO(STDOUT_FILENO, mode='w'))

    try:
        socket_path = os.environ.get(PAREC_PATH_ENV)
        if socket_path is None:
            while True:
                stdout.write(hunk)

        else:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(socket_path)

                return_code, do_sleep = handle_first_command(sock)

                while True:
                    # Get the 'count' value.
                    bcount = sock.recv(1024)
                    if not bcount:
                        return
                    count = int(bcount)
                    assert count % BLKSIZE == 0

                    # Write 'count' bytes.
                    for i in range(count // BLKSIZE):
                        stdout.write(hunk)
                    stdout.flush()

                    # Write the 'count' value.
                    sock.sendall(bcount)

    except Exception as e:
        print(f'parec stub error: {e!r}', file=sys.stderr)
        return_code = 1
    finally:
        stdout.close()
        return return_code

def encoder():
    """Write forever 'count' bytes read from stdin to stdout."""

    print('encoder stub starting', file=sys.stderr)
    stdin = io.BufferedReader(io.FileIO(STDIN_FILENO, mode='r'))
    stdout = io.BufferedWriter(io.FileIO(STDOUT_FILENO, mode='w'))

    try:
        socket_path = os.environ[ENCODER_PATH_ENV]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(socket_path)

            return_code, do_sleep = handle_first_command(sock)

            while True:
                # Get the 'count' value.
                bcount = sock.recv(1024)
                if not bcount:
                    break
                count = int(bcount)

                # Write 'count' bytes.
                data = stdin.read(count)
                stdout.write(data)
                stdout.flush()

                # Write the 'count' value.
                sock.sendall(bcount)

            # Sleep to let the Track.run() task be cancelled by Track.stop().
            if do_sleep:
                time.sleep(10)

    except Exception as e:
        print(f'encoder stub error: {e!r}', file=sys.stderr)
        return_code = 1
    finally:
        stdin.close()
        stdout.close()
        print(f'encoder stub return_code: {return_code}', file=sys.stderr)
        return return_code

def main():
    processes = {
        'parec': parec,
        'encoder': encoder
        }

    proc_name = os.path.basename(sys.argv[0])
    return processes[proc_name]()

if __name__ == '__main__':
    sys.exit(main())
