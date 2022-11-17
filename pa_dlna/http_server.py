"""An asyncio HHTP 1.1 server serving DLNA devices requests."""

import os
import io
import asyncio
import signal
import http.server
import urllib.parse
import logging
from http import HTTPStatus

from . import pprint_pformat
from .upnp import AsyncioTasks
from .encoders import FFMpegEncoder, L16Encoder

logger = logging.getLogger('http')

# A stream with a throughput of 1 Mbpss sends 2048 bytes every 15.6 msecs.
HTTP_CHUNK_SIZE = 2048

async def kill_process(process):
    try:
        try:
            # First try with SIGTERM.
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        finally:
            # Kill the process if the process is still alive.
            # And close transports of stdin, stdout, and stderr pipes,
            # otherwise we would get an exception on exit triggered by garbage
            # collection (a Python bug ?):
            # Exception ignored in: <function BaseSubprocessTransport.__del__:
            #   RuntimeError: Event loop is closed
            process._transport.close()
    except ProcessLookupError as e:
        logger.debug(f"Ignoring exception: '{e!r}'")

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

class Stream:
    """An HTTP socket connected to a subprocess stdout."""

    def __init__(self, session, writer):
        self.session = session
        self.writer = writer
        self.task = None
        self.closing = False

    async def shutdown(self):
        """Close the HTTP socket."""

        if self.writer is None:
            return
        writer = self.writer
        self.writer = None

        try:
            # Write the last chunk.
            if not writer.is_closing():
                writer.write('0\r\n\r\n'.encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        except asyncio.CancelledError:
            logger.debug('Got CancelledError during Stream shutdown')
        except Exception as e:
            logger.debug(f'Got exception during Stream shutdown: {e!r}')

    def stop(self):
        """Run the shutdown coro in a task.

        This method is meant to be run from another non-Stream task.
        """

        if not self.closing:
            self.closing = True
            self.task.cancel()

    async def close(self):
        if not self.closing:
            self.closing = True
            await self.shutdown()

    async def write_stream(self, reader):
        """Write to the Stream Writer what is read from a subprocess stdout."""

        logger = logging.getLogger('writer')
        rdr_name = self.session.renderer.name
        while True:
            partial_data = False
            if self.writer.is_closing():
                logger.debug(f'{rdr_name}: socket is closing')
                break
            try:
                data = await reader.readexactly(HTTP_CHUNK_SIZE)
            except asyncio.IncompleteReadError as e:
                data = e.partial
                partial_data = True
            if data:
                self.writer.write(f'{HTTP_CHUNK_SIZE:x}\r\n'.encode())
                self.writer.write(data)
                self.writer.write('\r\n'.encode())
                await self.writer.drain()
            if not data or partial_data:
                logger.debug(f'EOF reading from pipe on {rdr_name}')
                break

    async def run(self, reader):
        renderer = self.session.renderer
        try:
            query = ['HTTP/1.1 200 OK',
                     'Content-type: ' + renderer.mime_type,
                     'Connection: close',
                     'Transfer-Encoding: chunked',
                     '', '']
            self.writer.write('\r\n'.join(query).encode('latin-1'))
            await self.writer.drain()
            await self.write_stream(reader)
        except asyncio.CancelledError:
            self.session.stream_tasks.create_task(self.shutdown(),
                                                  name='shutdown')
        except ConnectionError as e:
            logger.info(f'{renderer.name} HTTP socket is closed: {e!r}')
            await self.close()
            await renderer.disable_temporary()
        except Exception as e:
            logger.exception(f'{e!r}')
        finally:
            await self.close()

class StreamProcesses:
    """Processes connected through pipes to an HTTP socket.

        - 'parec' records the audio from the nullsink monitor and pipes it
          to the encoder program.
        - The encoder program encodes the audio according to the encoder
          protocol and forwards it to the Stream instance.
        - The Stream instance writes the stream to the HTTP socket.
    """

    def __init__(self, session):
        self.session = session
        self.parec_proc = None
        self.encoder_proc = None
        self.pipe_reader = -1       # reader of the parec to encoder pipe
        self.stream_reader = None   # StreamReader end of the pipe chain
        self.no_encoder = isinstance(session.renderer.encoder, L16Encoder)
        self.queue = asyncio.Queue()

    async def close_encoder(self):
        if self.encoder_proc is not None:
            # Prevent verbose error logs from ffmpeg upon SIGTERM.
            if isinstance(self.session.renderer.encoder, FFMpegEncoder):
                for task in self.session.stream_tasks:
                    if task.get_name() == 'encoder_stderr':
                        task.cancel()
                        break
            await kill_process(self.encoder_proc)
            self.encoder_proc = None
            self.stream_reader = None

    async def close(self, disable=False):
        renderer = self.session.renderer
        logger.info(f'Terminate the {renderer.name} stream processes')
        try:
            if self.parec_proc is not None:
                await kill_process(self.parec_proc)
                os.close(self.pipe_reader)
                self.parec_proc = None

            await self.close_encoder()

            if disable:
                await renderer.disable_root_device()

        except Exception as e:
            logger.exception(f'{e!r}')

    async def get_stream_reader(self):
        """Get the stdout pipe of the last process."""

        if self.stream_reader is None:
            self.stream_reader = await self.queue.get()
        return self.stream_reader

    async def log_stderr(self, name, stderr):
        logger = logging.getLogger(name)

        renderer = self.session.renderer
        remove_env = False
        if (name == 'encoder' and
                isinstance(renderer.encoder, FFMpegEncoder) and
                'AV_LOG_FORCE_NOCOLOR' not in os.environ):
            os.environ['AV_LOG_FORCE_NOCOLOR'] = '1'
            remove_env = True
        try:
            while True:
                msg = await stderr.readline()
                if msg == b'':
                    break
                logger.error(msg.decode().strip())
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f'{e!r}')
        finally:
            if remove_env:
                del os.environ['AV_LOG_FORCE_NOCOLOR']

    async def run_parec(self, encoder, parec_pgm, stdout=None):
        renderer = self.session.renderer
        try:
            if self.no_encoder:
                format = encoder._network_format
                stdout = asyncio.subprocess.PIPE
            else:
                format = encoder._pulse_format
            monitor = renderer.nullsink.sink.monitor_source_name
            parec_cmd = [parec_pgm, f'--device={monitor}',
                         f'--format={format}',
                         f'--rate={encoder.rate}',
                         f'--channels={encoder.channels}']
            logger.debug(f"{renderer.name}: {' '.join(parec_cmd)}")

            exit_status = 0
            self.parec_proc = await asyncio.create_subprocess_exec(
                                    *parec_cmd,
                                    stdin=asyncio.subprocess.DEVNULL,
                                    stdout=stdout,
                                    stderr=asyncio.subprocess.PIPE)

            if self.no_encoder:
                self.queue.put_nowait(self.parec_proc.stdout)
            else:
                os.close(stdout)
            self.session.stream_tasks.create_task(
                        self.log_stderr('parec', self.parec_proc.stderr),
                        name='parec_stderr')

            ret = await self.parec_proc.wait()
            self.parec_proc = None
            exit_status = ret if ret >= 0 else signal.strsignal(-ret)
            logger.debug(f'Exit status of parec process: {exit_status}')
            if exit_status in (0, 'Terminated'):
                await self.close()
                return
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f'{e!r}')

        await self.close(disable=True)

    async def run_encoder(self, encoder_cmd):
        renderer = self.session.renderer
        try:
            logger.debug(f"{renderer.name}: {' '.join(encoder_cmd)}")

            exit_status = 0
            self.encoder_proc = await asyncio.create_subprocess_exec(
                                    *encoder_cmd,
                                    stdin=self.pipe_reader,
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE)
            self.queue.put_nowait(self.encoder_proc.stdout)
            self.session.stream_tasks.create_task(
                    self.log_stderr('encoder', self.encoder_proc.stderr),
                    name='encoder_stderr')

            ret = await self.encoder_proc.wait()
            self.encoder_proc = None
            exit_status = ret if ret >= 0 else signal.strsignal(-ret)
            # ffmpeg exit code is 255 when the process is killed with SIGTERM.
            # See ffmpeg main() at https://gitlab.com/fflabs/ffmpeg/-/blob/
            # 0279e727e99282dfa6c7019f468cb217543be243/fftools/ffmpeg.c#L4833
            if (isinstance(renderer.encoder, FFMpegEncoder) and
                    exit_status == 255):
                exit_status = 'Terminated'
            logger.debug(f'Exit status of encoder process: {exit_status}')

            if exit_status in (0, 'Terminated'):
                return
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f'{e!r}')

        await self.close(disable=True)

    async def run(self):
        renderer = self.session.renderer
        logger.info(f'Start the {renderer.name} stream processes')
        encoder = renderer.encoder
        try:
            if self.parec_proc is None:
                # Start the parec task.
                # An L16Encoder stream only runs the parec program.
                parec_pgm = renderer.control_point.parec_pgm
                if self.no_encoder:
                    coro = self.run_parec(encoder, parec_pgm)
                else:
                    self.pipe_reader, stdout = os.pipe()
                    coro = self.run_parec(encoder, parec_pgm, stdout)
                self.session.stream_tasks.create_task(coro, name='parec')

            # Start the encoder task.
            if not self.no_encoder and self.encoder_proc is None:
                encoder_cmd = encoder.command
                self.session.stream_tasks.create_task(
                                self.run_encoder(encoder_cmd),
                                name='encoder')
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f'{e!r}')
            await self.close(disable=True)

class StreamSession:
    """Handle the stream subprocesses and its HTTP socket.

    Stopping the stream terminates the encoder process but not the
    parec process.
    """

    def __init__(self, renderer):
        self.renderer = renderer
        self.processes = None
        self.stream = None
        self.stream_tasks = AsyncioTasks()

    async def stop_stream(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream = None
            await self.processes.close_encoder()

    async def close(self):
        await self.stop_stream()
        if self.processes is not None:
            await self.processes.close()
            self.processes = None

    async def start_stream(self, writer):
        # Start the subprocesses.
        if self.processes is None:
            self.processes = StreamProcesses(self)
        await self.processes.run()

        # Get the reader from the last subprocess on the pipe chain.
        reader = await self.processes.get_stream_reader()
        self.stream = Stream(self, writer)
        self.stream.task = self.stream_tasks.create_task(
                                        self.stream.run(reader),
                                        name='stream')

    def is_playing(self):
        return self.stream is not None and self.stream.writer is not None

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
                if renderer.stream_session.is_playing():
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
