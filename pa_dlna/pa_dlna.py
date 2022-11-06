"""An UPnP control point routing PulseAudio streams to DLNA devices."""

import sys
import os
import shutil
import asyncio
import logging
import re
import ipaddress
import random
from signal import strsignal, SIGINT, SIGTERM
from collections import namedtuple

from . import main_function, UPnPApplication
from .pulseaudio import Pulse
from .http_server import HTTPServer, run_httpserver
from .encoders import select_encoder, FFMpegEncoder, L16Encoder
from .upnp import (UPnPControlPoint, UPnPClosedDeviceError, AsyncioTasks,
                   UPnPSoapFaultError, NL_INDENT, shorten)

logger = logging.getLogger('pa-dlna')

AUDIO_URI_PREFIX = '/audio-content'
MEDIARENDERER = 'urn:schemas-upnp-org:device:MediaRenderer:'
AVTRANSPORT = 'urn:upnp-org:serviceId:AVTransport'
RENDERINGCONTROL = 'urn:upnp-org:serviceId:RenderingControl'
CONNECTIONMANAGER = 'urn:upnp-org:serviceId:ConnectionManager'
IGNORED_SOAPFAULTS = {'701': 'Transition not available',
                      '715': "Content 'BUSY'"}
# Period in seconds during which the renderer is disabled after the stream
# has been closed by the DLNA device.
RENDERER_DISABLE_PERIOD = 20
# A stream with a throughput of 1 Mbs sends 2048 bytes every 15.6 msecs.
HTTP_CHUNK_SIZE = 2048

UPnPAction = namedtuple('UPnPAction', ['action', 'state'])
random.seed()
def get_udn():
    """Build a random UPnP udn."""

    rbytes = random.randbytes(16)
    p = 0
    udn = ['uuid:']
    for n in [4, 2, 2, 2, 6]:
        if p != 0:
            udn.append('-')
        udn.append(''.join(format(x, '02x') for x in rbytes[p:p+n]))
        p += n
    return ''.join(udn)

async def close_aiostream(writer):
    try:
        # Write the last chunk.
        if not writer.is_closing():
            writer.write('0\r\n\r\n'.encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass

async def kill_process(process):
    # First try a plain termination.
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        process.kill()

def log_action(name, action, state, ignored=False, msg=None):
    txt = f"'{action}' "
    if ignored:
        txt += 'ignored '
    txt += f'UPnP action [{name} device prev state: {state}]'
    if msg is not None:
        txt += NL_INDENT + msg
    logger.debug(txt)

# Classes.
class MetaData(namedtuple('MetaData', ['publisher', 'artist', 'title'])):
    def __str__(self):
        return shorten(repr(self), head_len=40, tail_len=40)

class Stream:
    """An audio stream.

    The stream is made of two processes and an asyncio Stream Writer connected
    through pipes:
        - 'parec' records the audio from the nullsink monitor and pipes it
          to the encoder program.
        - The encoder program encodes the audio according to the encoder
          protocol and forwards it to the Stream Writer.
        - The Stream Writer writes the stream to the HTTP socket.
    """

    def __init__(self, renderer):
        self.renderer = renderer
        self.writer = None
        self.parec_proc = None
        self.encoder_proc = None
        self.stream_tasks = AsyncioTasks()

    async def stop(self):
        """Stop the stream and instantiate a new one."""

        if self.writer is None:
            return

        writer = self.writer
        # Prevent recursing in Stream.stop() and tell the task running the
        # Stream.write_aiostream() coroutine to terminate.
        self.writer = None
        renderer = self.renderer

        # Instantiate a new Stream.
        logger.info(f'Terminate the {renderer.name} stream processes')
        renderer.stream = Stream(renderer)

        await close_aiostream(writer)
        try:
            if self.parec_proc is not None:
                await kill_process(self.parec_proc)

            if self.encoder_proc is not None:
                # Prevent verbose error logs from ffmpeg upon SIGTERM.
                if isinstance(renderer.encoder, FFMpegEncoder):
                    for task in self.stream_tasks:
                        if task.get_name() == 'encoder_stderr':
                            task.cancel()
                            break
                await kill_process(self.encoder_proc)
        except Exception as e:
            logger.exception(f'{e!r}')

    async def disable_renderer(self):
        """Disable temporarily the renderer."""

        renderer = self.renderer
        nullsink = renderer.nullsink
        # Pulse events related to this sink are now discarded.
        renderer.nullsink = None

        # Stop the stream.
        await self.stop()

        # Unload the null-sink module, sleep RENDERER_DISABLE_PERIOD
        # seconds and load a new module. During  the sleep period, the
        # stream that was routed to this null-sink will be routed to
        # the default sink instead of being silently discarded by the
        # null-sink.
        if nullsink is not None:
            pulse = renderer.control_point.pulse
            await pulse.unregister(nullsink)
            logger.info(f'Wait {RENDERER_DISABLE_PERIOD} seconds before'
                        f' re-enabling {renderer.name}')
            await asyncio.sleep(RENDERER_DISABLE_PERIOD)
            nullsink = await pulse.register(renderer, renderer.name)
            if nullsink is not None:
                renderer.nullsink = nullsink
            else:
                logger.error(f'Cannot load a new null-sink module'
                             f' for {renderer.name}')
                await self.close()

    async def close(self):
        """Stop the stream and disable permanently the root device."""

        await self.stop()
        await self.renderer.disable_root_device()

    async def write_aiostream(self, stdout):
        """Write to the Stream Writer what is read from a subprocess stdout."""

        logger = logging.getLogger('writer')
        rdr_name = self.renderer.name
        try:
            while True:
                if self.writer is None:
                    return
                if self.writer.is_closing():
                    logger.debug(f'{rdr_name}: socket is closing')
                    break
                data = await stdout.readexactly(HTTP_CHUNK_SIZE)
                if self.writer is None:
                    return
                if not data:
                    logger.debug(f'EOF reading from pipe on {rdr_name}')
                    break
                self.writer.write(f'{HTTP_CHUNK_SIZE:x}\r\n'.encode())
                self.writer.write(data)
                self.writer.write('\r\n'.encode())
                await self.writer.drain()
        except (asyncio.CancelledError, asyncio.IncompleteReadError):
            pass
        except ConnectionError as e:
            logger.info(f'{rdr_name} HTTP socket is closed: {e!r}')
            await self.disable_renderer()
            return
        except Exception as e:
            logger.exception(f'{e!r}')
            await self.close()
            return

        await self.stop()

    async def log_stderr(self, name, stderr):
        logger = logging.getLogger(name)

        remove_env = False
        if (name == 'encoder' and
                isinstance(self.renderer.encoder, FFMpegEncoder) and
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
        try:
            if not isinstance(encoder, L16Encoder):
                format = encoder._pulse_format
            else:
                format = encoder._network_format
                stdout = asyncio.subprocess.PIPE
            monitor = self.renderer.nullsink.sink.monitor_source_name
            parec_cmd = [parec_pgm, f'--device={monitor}',
                         f'--format={format}',
                         f'--rate={encoder.rate}',
                         f'--channels={encoder.channels}']
            logger.info(f"{self.renderer.name}: {' '.join(parec_cmd)}")

            exit_status = 0
            self.parec_proc = await asyncio.create_subprocess_exec(
                                    *parec_cmd,
                                    stdin=asyncio.subprocess.DEVNULL,
                                    stdout=stdout,
                                    stderr=asyncio.subprocess.PIPE)

            if not isinstance(encoder, L16Encoder):
                os.close(stdout)
            else:
                self.stream_tasks.create_task(self.write_aiostream(
                                                    self.parec_proc.stdout),
                                              name='parec_writer')
            self.stream_tasks.create_task(self.log_stderr('parec',
                                                    self.parec_proc.stderr),
                                          name='parec_stderr')

            ret = await self.parec_proc.wait()
            exit_status = ret if ret >= 0 else strsignal(-ret)
            logger.debug(f'Exit status of parec process: {exit_status}')
            self.parec_proc = None
            if exit_status in (0, 'Terminated'):
                await self.stop()
                return
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f'{e!r}')

        await self.close()

    async def run_encoder(self, encoder_cmd, pipe_r):
        try:
            logger.info(f"{self.renderer.name}: {' '.join(encoder_cmd)}")

            exit_status = 0
            self.encoder_proc = await asyncio.create_subprocess_exec(
                                    *encoder_cmd,
                                    stdin=pipe_r,
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE)
            os.close(pipe_r)
            self.stream_tasks.create_task(self.write_aiostream(
                                                self.encoder_proc.stdout),
                                          name='encoder_writer')
            self.stream_tasks.create_task(self.log_stderr('encoder',
                                                    self.encoder_proc.stderr),
                                          name='encoder_stderr')

            ret = await self.encoder_proc.wait()
            exit_status = ret if ret >= 0 else strsignal(-ret)
            # ffmpeg exit code is 255 when the process is killed with SIGTERM.
            # See ffmpeg main() at https://gitlab.com/fflabs/ffmpeg/-/blob/
            # 0279e727e99282dfa6c7019f468cb217543be243/fftools/ffmpeg.c#L4833
            if (isinstance(self.renderer.encoder, FFMpegEncoder) and
                    exit_status == 255):
                exit_status = 'Terminated'
            logger.debug(f'Exit status of encoder process: {exit_status}')

            self.encoder_proc = None
            if exit_status in (0, 'Terminated'):
                await self.stop()
                return
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f'{e!r}')

        await self.close()

    async def start(self, writer):
        renderer = self.renderer
        try:
            self.writer = writer

            logger.info(f'Start the {renderer.name} stream  processes')
            query = ['HTTP/1.1 200 OK',
                     'Content-type: ' + renderer.mime_type,
                     'Connection: close',
                     'Transfer-Encoding: chunked',
                     '', '']
            writer.write('\r\n'.join(query).encode('latin-1'))
            await writer.drain()

            # Start the parec task.
            # An L16Encoder stream only runs the parec program.
            encoder = renderer.encoder
            parec_pgm = renderer.control_point.parec_pgm
            if isinstance(encoder, L16Encoder):
                coro = self.run_parec(encoder, parec_pgm)
            else:
                pipe_r, stdout = os.pipe()
                coro = self.run_parec(encoder, parec_pgm, stdout)
            self.stream_tasks.create_task(coro, name='parec')

            # Start the encoder task.
            if not isinstance(encoder, L16Encoder):
                encoder_cmd = encoder.command
                self.stream_tasks.create_task(self.run_encoder(encoder_cmd,
                                                               pipe_r),
                                              name='encoder')
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f'{e!r}')
            await self.close()

class Renderer:
    """A DLNA MediaRenderer.

    Attributes:
      net_iface     The control point ipaddress.IPv4Interface network
                    interface that the DLNA device belongs to

    See the Standardized DCP (SDCP) specifications:
      'AVTransport:3 Service'
      'RenderingControl:3 Service'
      'ConnectionManager:3 Service'
    """

    def __init__(self, control_point, net_iface, root_device):
        self.control_point = control_point
        self.net_iface = net_iface
        self.root_device = root_device
        self.closing = False
        self.nullsink = None            # NullSink instance
        self.name = None                # NullSink name
        self.previous_idx = None        # index of previous sink input
        self.encoder = None
        self.mime_type = None
        self.protocol_info = None
        self.current_uri = None
        self.new_pulse_session = False
        self.stream = Stream(self)
        self.pulse_queue = asyncio.Queue()

    async def close(self):
        if not self.closing:
            self.closing = True
            logger.info(f'Close {self.name} renderer')
            if self.nullsink is not None:
                await self.control_point.pulse.unregister(self.nullsink)
                self.nullsink = None
            await self.stream.stop()

            # Closing the root device will trigger a 'byebye' notification and
            # the renderer will be removed from self.control_point.renderers.
            self.root_device.close()

    async def disable_root_device(self):
        """Close the renderer and disable its root device."""

        await self.close()
        self.control_point.disable_root_device(self)

    async def register(self):
        nullsink = await self.control_point.pulse.register(self)
        if nullsink is not None:
            self.nullsink = nullsink
            self.name = nullsink.sink.name
            return True

    def match(self, uri_path):
        return uri_path == f'{AUDIO_URI_PREFIX}/{self.root_device.udn}'

    def start_stream(self, writer):
        task_name = f'stream-{self.name}'
        self.control_point.cp_tasks.create_task(
                            self.stream.start(writer), name=task_name)

    def pulse_states(self, sink):
        if sink is None:
            sink = self.nullsink.sink
            prev_state = sink.state._value
            new_state = None
        else:
            prev_sink = self.nullsink.sink
            prev_state = (prev_sink.state._value
                          if prev_sink is not None else None)
            new_state = sink.state._value
        return prev_state, new_state

    def log_pulse_event(self, event, prev_state, new_state, sink_input):
        if new_state is None:
            sink_state = f'previous state: {prev_state}'
        else:
            sink_state = f'prev/new state: {prev_state}/{new_state}'

        if sink_input is None:
            sink_input = self.nullsink.sink_input

        logger.debug(f"'{event}' pulseaudio event [{self.name} "
                     f'sink: idx {sink_input.index}, {sink_state}]')

    def sink_input_meta(self, sink_input):
        if sink_input is None:
            return None

        proplist = sink_input.proplist
        publisher = proplist.get('application.name', '')
        artist = proplist.get('media.artist', '')
        title = proplist.get('media.title', '')

        if not self.encoder.http_per_track:
            title = publisher
            artist = ''
        # So that the device may display at least some useful info.
        elif not title:
            title = publisher

        return MetaData(publisher, artist, title)

    async def handle_action(self, action):
        # An action is either 'Play', 'Stop', 'Pause' or # an instance of
        # MetaData.

        # Get the stream state.
        timeout = 1.0
        try:
            state = await asyncio.wait_for(self.get_transport_state(),
                                           timeout=timeout)
        except asyncio.TimeoutError:
            state = ('PLAYING' if self.stream.writer is not None else
                     'STOPPED')
            logger.debug(f'{self.name} stream state: {state} '
                         f'(GetTransportInfo timed out after {timeout}'
                         f' second)')

        # Run an AVTransport action if needed.
        try:
            if state in ('PLAYING', 'TRANSITIONING'):
                if (self.encoder.http_per_track and
                        isinstance(action, MetaData)):
                    await self.set_avtnextransporturi(self.name,
                                                      action, state)
                    return
                elif action == 'Stop':
                    log_action(self.name, action, state)
                    await self.make_transition('Stop')
                    return
                # Ignore 'Pause' events as it does not work well with
                # streaming because of the DLNA buffering the stream.
                # Also pulseaudio generate very short lived 'Pause'
                # events that are annoying.
                elif action == 'Pause':
                    pass
            else:
                if isinstance(action, MetaData):
                    await self.set_avtransporturi(self.name, action,
                                                  state)
                    return
                elif action == 'Play':
                    log_action(self.name, action, state)
                    await self.make_transition('Play', speed=1)
                    return
        except UPnPSoapFaultError as e:
            error_code = e.args[0].errorCode
            if error_code in IGNORED_SOAPFAULTS:
                error_msg = IGNORED_SOAPFAULTS[error_code]
                logger.warning(f"Ignoring SOAP error '{error_msg}'")
            else:
                raise

        if isinstance(action, MetaData):
            log_action(self.name, 'SetAVTransportURI', state,
                       ignored=True, msg=str(action))
        else:
            log_action(self.name, action, state, ignored=True)

    async def handle_pulse_event(self):
        """Handle a PulseAudio event.

        This method is run by the 'pulse' task.
        'self.nullsink' holds the state prior to this event. The 'sink' and
        'sink_input' arguments define the new state.
        """

        event, sink, sink_input = await self.pulse_queue.get()

        assert self.nullsink is not None
        prev_state, new_state = self.pulse_states(sink)
        self.log_pulse_event(event, prev_state, new_state, sink_input)

        # Note that, at each pulseaudio event, a new instance of sink and
        # sink_input is generated by the pulsectl library.
        #
        # Ignore pulse events from a previous sink input.
        # These events are generated by pulseaudio after the user starts a new
        # song.
        if sink_input is not None:
            if sink_input.index == self.previous_idx:
                logger.debug(f"'{event}' ignored pulseaudio event related to"
                             f' previous sink-input of {self.name}')
                return

            if (self.nullsink.sink_input is not None  and
                    sink_input.index != self.nullsink.sink_input.index):
                self.previous_idx = self.nullsink.sink_input.index

        actions = []
        # Process the event and set the new attributes values of nullsink.
        if event in ('remove', 'exit'):
            actions.append('Stop')
            self.nullsink.sink_input = None
        else:
            assert sink is not None and sink_input is not None
            if event == 'new':
                self.new_pulse_session = True

            # Handle the metadata first: when the device current state is
            # 'STOPPED', if a 'Play' action is incorrectly pushed first on the
            # pulse_queue then a 'SetNextAVTransportURI' action is triggered
            # instead of the correct 'SetAVTransportURI' action.
            cur_metadata = self.sink_input_meta(sink_input)
            if (self.new_pulse_session and
                    (prev_state, new_state) == ('idle', 'running')):
                actions.append(cur_metadata)
                self.new_pulse_session = False
            elif (prev_state, new_state) == ('running', 'running'):
                prev_metadata = self.sink_input_meta(self.nullsink.sink_input)
                if cur_metadata is not None and cur_metadata != prev_metadata:
                    actions.append(cur_metadata)

            if (new_state == 'running' and
                        (prev_state != 'running' or
                         self.nullsink.sink_input is None)):
                    actions.append('Play')
            elif (prev_state, new_state) == ('running', 'idle'):
                actions.append('Pause')

            self.nullsink.sink = sink
            self.nullsink.sink_input = sink_input

        for action in actions:
            await self.handle_action(action)

    async def soap_action(self, serviceId, action, args={}):
        """Send a SOAP action.

        Return the dict {argumentName: out arg value} if successfull,
        otherwise an instance of the upnp.xml.SoapFault namedtuple defined by
        field names in ('errorCode', 'errorDescription').
        """

        try:
            service = self.root_device.serviceList[serviceId]
            return await service.soap_action(action, args, log_debug=False)
        except UPnPClosedDeviceError:
            logger.error(f'soap_action(): root device {self.root_device} is'
                         f' closed')

    async def select_encoder(self, udn):
        """Select an encoder matching the DLNA device supported mime types."""

        protocol_info = await self.soap_action(CONNECTIONMANAGER,
                                               'GetProtocolInfo')
        res = select_encoder(self.control_point.config, self.name,
                             protocol_info, udn)
        if res is None:
            logger.error(f'Cannot find an encoder matching the {self.name}'
                         f' supported mime types')
            await self.disable_root_device()
            return False
        self.encoder, self.mime_type, self.protocol_info = res
        return True

    def didl_lite_metadata(self, metadata):
        """Build de didl-lite xml string.

        The returned string is built with ../tools/build_didl_lite.py.
        """

        metadata = (
          f'''
        <DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
          xmlns:dc="http://purl.org/dc/elements/1.1/"
          xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
        <item id="0" parentID="0" restricted="0">
          <dc:title>{metadata.title}</dc:title>
          <upnp:class>object.item.audioItem.musicTrack</upnp:class>
          <dc:publisher>{metadata.publisher}</dc:publisher>
          <upnp:artist>{metadata.artist}</upnp:artist>
          <res protocolInfo="{self.protocol_info}">
            {self.current_uri}</res>
        </item></DIDL-Lite>
          '''
        )
        return metadata.strip()

    async def set_avtransporturi(self, name, metadata, state):
        action = 'SetAVTransportURI'
        didl_lite_metadata = self.didl_lite_metadata(metadata)
        args = {'InstanceID': 0,
                'CurrentURI': self.current_uri,
                'CurrentURIMetaData': didl_lite_metadata
                }
        log_action(name, action, state, msg=didl_lite_metadata)
        logger.info(f'{metadata}'
                    f'{NL_INDENT}URL: {self.current_uri}')
        await self.soap_action(AVTRANSPORT, action, args)

    async def set_avtnextransporturi(self, name, metadata, state):
        action = 'SetNextAVTransportURI'
        didl_lite_metadata = self.didl_lite_metadata(metadata)
        args = {'InstanceID': 0,
                'NextURI': self.current_uri,
                'NextURIMetaData': didl_lite_metadata
                }

        await self.stream.stop()
        log_action(name, action, state, msg=didl_lite_metadata)
        logger.info(f'{metadata}'
                    f'{NL_INDENT}URL: {self.current_uri}')
        await self.soap_action(AVTRANSPORT, action, args)

    async def get_transport_state(self):
        res = await self.soap_action(AVTRANSPORT, 'GetTransportInfo',
                                     {'InstanceID': 0})
        state = res['CurrentTransportState']
        return state

    async def make_transition(self, transition, speed=None):
        args = {'InstanceID': 0}
        if speed is not None:
            args['Speed'] = speed

        if transition == 'Stop':
            await self.stream.stop()

        await self.soap_action(AVTRANSPORT, transition, args)

    async def run(self):
        """Run the Renderer task."""

        try:
            udn = self.root_device.udn
            if not await self.select_encoder(udn):
                await self.disable_root_device()
                return
            self.current_uri = (f'http://{self.net_iface.ip}'
                                f':{self.control_point.port}'
                                f'{AUDIO_URI_PREFIX}/{udn}')
            logger.info(f'New {self.name} renderer with {self.encoder}'
                        f" handling '{self.mime_type}'")

            while True:
                await self.handle_pulse_event()

        except asyncio.CancelledError:
            await self.close()
        except Exception as e:
            logger.exception(f'{e!r}')
            await self.disable_root_device()

class TestRenderer(Renderer):
    """Non UPnP Renderer to be used for testing."""

    LOOPBACK = ipaddress.IPv4Interface('127.0.0.1/8')

    class RootDevice:

        count = 0

        def __init__(self, renderer, mime_type, control_point):
            self.control_point = control_point
            self.renderer = renderer
            self.udn = get_udn()
            self.ip_source = '127.0.0.1'

            try:
                name = mime_type.split('/')[1]
            except IndexError:
                name = 'audio'
            TestRenderer.RootDevice.count += 1
            self.modelName = f'DLNA-{name}-{self.count}'
            self.friendlyName = self.modelName + ' test device'

        def close(self):
            self.control_point.renderers.remove(self.renderer)

    def __init__(self, control_point, mime_type):
        super().__init__(control_point, self.LOOPBACK,
                         self.RootDevice(self, mime_type, control_point))
        self.mime_type = mime_type

    async def make_transition(self, transition, speed=None):
        if transition == 'Stop':
            await self.stream.stop()

    async def soap_action(self, serviceId, action, args):
        if action == 'GetProtocolInfo':
            return {'Source': None,
                    'Sink': f'http-get:*:{self.mime_type}:*'
                    }
        elif action == 'GetTransportInfo':
            state = 'PLAYING' if self.stream.writer is not None else 'STOPPED'
            return {'CurrentTransportState': state}

class AVControlPoint(UPnPApplication):
    """Control point with Content.

    Manage PulseAudio and the DLNA MediaRenderer devices.
    See section 6.6 of "UPnP AV Architecture:2".
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closing = False
        self.renderers = set()
        self.curtask = None             # task running run_control_point()
        self.pulse = None               # Pulse instance
        self.start_event = asyncio.Event()
        self.faulty_devices = set()     # set of the udn of root devices
                                        # having been disabled
        self.cp_tasks = AsyncioTasks()

    async def shutdown(self, end_event):
        try:
            await end_event.wait()
            await self.close()
        except Exception as e:
            logger.exception(f'{e!r}')
        finally:
            loop = asyncio.get_running_loop()
            for sig in (SIGINT, SIGTERM):
                loop.remove_signal_handler(sig)

    async def close(self):
        if not self.closing:
            self.closing = True
            for renderer in list(self.renderers):
                await renderer.close()

            if self.pulse is not None:
                await self.pulse.close()

            self.curtask.cancel()

    def disable_root_device(self, renderer):
        udn = renderer.root_device.udn
        if udn not in self.faulty_devices:
            logger.info(f'Disable the {renderer.name} device permanently')
            self.faulty_devices.add(udn)

    async def register(self, renderer, http_server):
        """Load the null-sink module and create the renderer task."""

        if await renderer.register():
            http_server.allow_from(renderer.root_device.ip_source)
            self.renderers.add(renderer)
            self.cp_tasks.create_task(renderer.run(),
                                      name=renderer.nullsink.sink.name)

    async def handle_upnp_notifications(self, upn_control_point, http_server):
        while True:
            notif, root_device = await upn_control_point.get_notification()
            logger.info(f"Got '{notif}' notification for {root_device}")

            # Ignore non Renderer devices.
            if re.match(rf'{MEDIARENDERER}(1|2)',
                        root_device.deviceType) is None:
                continue

            # Find an existing Renderer instance.
            for rndr in self.renderers:
                if rndr.root_device is root_device:
                    renderer = rndr
                    break
            else:
                renderer = None

            if notif == 'alive':
                if root_device.udn in self.faulty_devices:
                    assert renderer is None
                    logger.info(f'Ignore disabled {root_device}')
                    continue

                if renderer is None:
                    # Check that ip_source belongs to one of the
                    # net_ifaces networks.
                    ip_source = root_device.ip_source
                    ip_obj = ipaddress.IPv4Address(ip_source)
                    for net_iface in self.net_ifaces:
                        if ip_obj in net_iface.network:
                            break
                    else:
                        logger.warning(f'{ip_source} does not belong to one'
                                       f' of the enabled networks')
                        continue
                    rndr = Renderer(self, net_iface, root_device)
                    await self.register(rndr, http_server)
            else:
                if renderer is not None:
                    if not renderer.closing:
                        await renderer.close()
                    else:
                        self.renderers.remove(renderer)
                else:
                    logger.warning("Got a 'byebye' notification for no"
                                   ' existing Renderer')

    async def run_control_point(self):
        if not any(enc.available for enc in self.config.values()):
            sys.exit('Error: No encoder is available')

        self.parec_pgm = shutil.which('parec')
        if self.parec_pgm is None:
            sys.exit("Error: The pulseaudio 'parec' program cannot be found")

        try:
            self.curtask = asyncio.current_task()

            # Add the signal handlers.
            end_event = asyncio.Event()
            self.cp_tasks.create_task(self.shutdown(end_event),
                                      name='shutdown')
            loop = asyncio.get_running_loop()
            for sig in (SIGINT, SIGTERM):
                loop.add_signal_handler(sig, end_event.set)

            # Run the UPnP control point.
            async with UPnPControlPoint(self.net_ifaces,
                                        self.ttl) as upn_control_point:
                # Create the Pulse task.
                self.pulse = Pulse(self)
                self.cp_tasks.create_task(self.pulse.run(), name='pulse')

                # Wait for the connection to PulseAudio to be ready.
                await self.start_event.wait()

                # Create the http_server task.
                http_server = HTTPServer(self, self.net_ifaces,
                                         self.port)
                self.cp_tasks.create_task(run_httpserver(http_server),
                                          name='http_server')

                # Register the TestRenderers.
                for mtype in (x.strip() for x in
                              self.renderers_mtypes.split(',')):
                    if not mtype:
                        continue
                    rndr = TestRenderer(self, mtype)
                    await self.register(rndr, http_server)

                # Handle UPnP notifications for ever.
                await self.handle_upnp_notifications(upn_control_point, http_server)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f'Got exception {e!r}')
        finally:
            await self.close()

    def __str__(self):
        return 'pa-dlna'

# The main function.
if __name__ == '__main__':
    main_function(AVControlPoint, __doc__)
