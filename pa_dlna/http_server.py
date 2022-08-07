"""An asyncio HHTP 1.1 server serving DLNA devices requests."""

import io
import asyncio
import http.server
import logging
from http import HTTPStatus

from . import pprint_pformat

logger = logging.getLogger('http')

class HTTPRequestHandler(http.server.BaseHTTPRequestHandler):

    def __init__(self, reader, writer):
        self._reader = reader
        self.wfile = writer
        self.has_error = True

        addr = writer.get_extra_info('peername')
        self.client_address = addr

        # BaseHTTPRequestHandler invokes self.wfile.flush().
        def flush():
            pass
        setattr(writer, 'flush', flush)

    async def set_rfile(self):
        # Read the full HTTP request from the asyncio Stream into a BytesIO.
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
                    f'{self.client_address[0]}\n'
                    f"        uri path: '{self.path}'")
        logger.debug(f'Request headers:\n'
                     f"{pprint_pformat(dict(self.headers.items()))}")
        if self.request_version != 'HTTP/1.1':
            self.send_error(HTTPStatus.HTTP_VERSION_NOT_SUPPORTED)
        else:
            self.has_error = False

class HTTPServer:
    """See Hypertext Transfer Protocol -- HTTP/1.1 - RFC 2616."""

    http_server = None

    def __init__(self, renderers, port):
        self.renderers = renderers
        self.port = port
        HTTPServer.http_server = self

    @staticmethod
    async def client_connected(reader, writer):
        do_close = True
        try:
            handler = HTTPRequestHandler(reader, writer)
            await handler.set_rfile()
            handler.handle_one_request()

            # Start the stream in a new task if the GET request is valid and
            # the uri path matches one of the encoder's.
            if not handler.has_error:
                renderers = HTTPServer.http_server.renderers
                for renderer in renderers.values():
                    res = await renderer.start_stream(writer, handler.path)
                    if res is True:
                        do_close = False
                        return
                handler.send_error(HTTPStatus.NOT_FOUND)

            # Flush the error response.
            await writer.drain()

        except Exception as e:
            logger.exception(f'{e!r}')
        finally:
            if do_close:
                writer.close()
                await writer.wait_closed()

    async def run(self):
        self.server = await asyncio.start_server(self.client_connected,
                                '127.0.0.1', self.port)
        addrs = ', '.join(str(sock.getsockname())
                          for sock in self.server.sockets)
        logger.info(f'Serving HTTP requests on {addrs}')

        async with self.server:
            try:
                await self.server.serve_forever()
            except asyncio.CancelledError:
                pass
            finally:
                logger.info('HTTP server is closed')
