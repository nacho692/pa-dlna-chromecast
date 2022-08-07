"""An asyncio HHTP 1.1 server serving DLNA devices requests."""

import io
import asyncio
import http.server
import urllib.parse
import logging
from http import HTTPStatus

from . import pprint_pformat

logger = logging.getLogger('http')

class HTTPRequestHandler(http.server.BaseHTTPRequestHandler):

    def __init__(self, reader, writer, peername):
        self._reader = reader
        self.wfile = writer
        self.client_address = peername
        self.has_error = True

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
    """HHTP server accepting connections only from 'allowed_ips'.

    Reference: Hypertext Transfer Protocol -- HTTP/1.1 - RFC 2616.
    """

    http_server = None

    def __init__(self, renderers, ip_list, port):
        self.renderers = renderers
        self.networks = ip_list
        self.port = port
        self.allowed_ips = set()
        HTTPServer.http_server = self

    def allow_from(self, ip_addr):
        self.allowed_ips.add(ip_addr)

    @staticmethod
    async def client_connected(reader, writer):
        http_server = HTTPServer.http_server
        peername = writer.get_extra_info('peername')
        ip_source = peername[0]
        if ip_source not in http_server.allowed_ips:
            sockname = writer.get_extra_info('sockname')
            logger.warning(f'Discarded TCP connection from {ip_source} (not'
                           f' allowed) received on {sockname[0]}')
            writer.close()
            return

        do_close = True
        try:
            handler = HTTPRequestHandler(reader, writer, peername)
            await handler.set_rfile()
            handler.handle_one_request()

            # Start the stream in a new task if the GET request is valid and
            # the uri path matches one of the encoder's.
            if not handler.has_error:
                # BaseHTTPRequestHandler has decoded the received bytes as
                # 'iso-8859-1' encoded, now unquote the uri path.
                uri_path = urllib.parse.unquote(handler.path)

                renderers = http_server.renderers
                for renderer in renderers.values():
                    res = await renderer.start_stream(writer, uri_path)
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
                                                 self.networks, self.port)
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
