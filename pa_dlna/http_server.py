"""An asyncio HHTP 1.1 server serving DLNA devices requests."""

import io
import asyncio
import http.server
import urllib.parse
import logging
from http import HTTPStatus

from . import pprint_pformat

logger = logging.getLogger('http')

async def run_httpserver(server):
    aio_server = await asyncio.start_server(server.client_connected,
                                            server.networks, server.port)
    addrs = ', '.join(str(sock.getsockname())
                      for sock in aio_server.sockets)
    logger.info(f'Serve HTTP requests on {addrs}')

    async with aio_server:
        try:
            await aio_server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info('Close HTTP server')

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
                    f'{self.client_address[0]}')
        logger.debug(f"uri path: '{self.path}'")
        logger.debug(f'Request headers:\n'
                     f"{pprint_pformat(dict(self.headers.items()))}")

class HTTPServer:
    """HHTP server accepting connections only from 'allowed_ips'.

    Reference: Hypertext Transfer Protocol -- HTTP/1.1 - RFC 7230.
    """

    def __init__(self, control_point, net_ifaces, port):
        self.control_point = control_point
        self.networks = [str(iface.ip) for iface in net_ifaces]
        self.port = port
        self.allowed_ips = set()

    def allow_from(self, ip_addr):
        self.allowed_ips.add(ip_addr)

    async def client_connected(self, reader, writer):
        peername = writer.get_extra_info('peername')
        ip_source = peername[0]
        if ip_source not in self.allowed_ips:
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

            # BaseHTTPRequestHandler has decoded the received bytes as
            # 'iso-8859-1' encoded, now unquote the uri path.
            uri_path = urllib.parse.unquote(handler.path)

            for renderer in self.control_point.renderers:
                if not renderer.match(uri_path):
                    continue

                if handler.request_version != 'HTTP/1.1':
                    handler.send_error(
                                HTTPStatus.HTTP_VERSION_NOT_SUPPORTED)
                    await renderer.disable_root_device()
                    break
                if renderer.stream.writer is not None:
                    handler.send_error(HTTPStatus.CONFLICT,
                                       f'Cannot start {renderer.name} stream'
                                       f' (already running)')
                    break
                if renderer.nullsink is None:
                    handler.send_error(HTTPStatus.CONFLICT,
                                       f'{renderer.name} temporarily disabled')
                    break

                # Ok, handle the request.
                renderer.start_stream(writer)
                do_close = False
                return

            else:
                handler.send_error(HTTPStatus.NOT_FOUND,
                                   'Cannot find a matching renderer')

            # Flush the error response.
            await writer.drain()

        except Exception as e:
            logger.exception(f'{e!r}')
        finally:
            if do_close:
                writer.close()
                await writer.wait_closed()
