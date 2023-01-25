"""Utilities."""

import io
import asyncio
import functools
import logging
import pprint
import http.server

logger = logging.getLogger('util')

NL_INDENT = '\n        '

def shorten(txt, head_len=10, tail_len=5):
    if len(txt) <= head_len + 3 + tail_len:
        return txt
    return txt[:head_len] + '...' + txt[len(txt)-tail_len:]

def log_exception(logger):
    """A decorator logging exceptions occuring in a coroutine.

    Its purpose is to ensure that a task may not trigger unhandled exceptions
    in code that is running in the last except clause or in code running in
    the last finally clause. Otherwise the exception shows up only when the
    event loop terminates making the problem difficult to resolve.
    """

    def decorator(coro):
        @functools.wraps(coro)
        async def wrapper(*args, **kwargs):
            try:
                return await coro(*args, **kwargs)
            except Exception as e:
                logger.error(
                    f'Exception at end of {coro.__qualname__}(): {e!r}')
        return wrapper
    return decorator

class AsyncioTasks:
    """Save references to tasks, to avoid tasks being garbage collected.

    See Python github PR 29163 and the corresponding Python issues.
    """

    def __init__(self):
        self._tasks = set()

    def create_task(self, coro, name):
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._tasks.remove(t))
        return task

    def __iter__(self):
        for t in self._tasks:
            yield t

class HTTPRequestHandler(http.server.BaseHTTPRequestHandler):

    def __init__(self, reader, writer, peername):
        self._reader = reader
        self.wfile = writer
        self.client_address = peername

        # BaseHTTPRequestHandler invokes self.wfile.flush().
        def flush():
            pass
        setattr(writer, 'flush', flush)

    async def set_rfile(self):
        # Read the full HTTP request from the asyncio StreamReader into a
        # BytesIO.
        request = []
        while True:
            line = await self._reader.readline()
            request.append(line)
            if line in (b'\r\n', b'\n', b''):
                break
        self.rfile = io.BytesIO(b''.join(request))

    def log_message(self, format, *args):
        # Overriding log_message() that logs the errors.
        logger.error("%s - %s" % (self.client_address[0], format%args))

    def do_GET(self):
        logger.info(f'{self.request_version} GET request from '
                    f'{self.client_address[0]}')
        logger.debug(f"uri path: '{self.path}'")
        logger.debug(f'Request headers:\n'
                     f"{pprint.pformat(dict(self.headers.items()))}")

    do_POST = do_GET
