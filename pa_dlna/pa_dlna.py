"An UPnP control point forwarding PulseAudio/Pipewire streams to DLNA and Chromecast devices."

import sys
import os
import shutil
import asyncio
import logging
import re
import ast
import mimetypes
import hashlib
import time
import urllib.parse
from signal import SIGINT, SIGTERM
from collections import namedtuple, UserList

from . import SYSTEMD_LOG_LEVEL
from .init import padlna_main, UPnPApplication, ControlPointAbortError
from .pulseaudio import Pulse
from .http_server import StreamSessions, HTTPServer
from .encoders import select_encoder
from .chromecast import ChromecastProvider, CHROMECAST_MIME_TYPES
from .upnp import (UPnPControlPoint, UPnPDevice, UPnPClosedDeviceError,
                   UPnPSoapFaultError, NL_INDENT, shorten,
                   log_unhandled_exception, AsyncioTasks, QUEUE_CLOSED,
                   xml_escape)

logger = logging.getLogger('pa-dlna')

AUDIO_URI_PREFIX = '/audio-content'
ARTWORK_URI_PREFIX = '/artwork'
MEDIARENDERER = 'urn:schemas-upnp-org:device:MediaRenderer:'
AVTRANSPORT = 'urn:upnp-org:serviceId:AVTransport'
RENDERINGCONTROL = 'urn:upnp-org:serviceId:RenderingControl'
CONNECTIONMANAGER = 'urn:upnp-org:serviceId:ConnectionManager'
IGNORED_SOAPFAULTS = {'701': 'Transition not available',
                      '715': "Content 'BUSY'"}
# Maximum time before starting a new session while waiting for the second
# pulse event.
NEW_SESSION_MAX_DELAY = 1
ISSUE_48_TIMER = 2

def get_udn(data):
    """Build an UPnP udn."""

    # 'hexdigest' length is 40, we will use the first 32 characters.
    hexdigest = hashlib.sha1(data).hexdigest()
    p = 0
    udn = ['uuid:']
    for n in [8, 4, 4, 4, 12]:
        if p != 0:
            udn.append('-')
        udn.append(hexdigest[p:p+n])
        p += n
    return ''.join(udn)

def log_action(name, action, state, ignored=False, msg=None):
    txt = f"'{action}' "
    if ignored:
        txt += 'ignored '
    txt += f'UPnP action [{name} device prev state: {state}]'
    if msg is not None:
        txt += NL_INDENT + msg
    logger.debug(txt)

def normalize_xml(xml):
    """Convert a multi-lines xml string to one line, handling whitespaces.

    To support parsing by libexpat, see issue 29.
    """

    lines = []
    prev_end = None
    for line in xml.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if prev_end and prev_end != '>':
            line = ' ' + line
        prev_end = line[-1]
        lines.append(line)
    return ''.join(lines)


def normalize_stream_value(value):
    if value is None:
        return ''
    if isinstance(value, bytes):
        value = value.decode(errors='ignore')
    elif isinstance(value, (list, tuple)):
        value = ', '.join(str(item).strip() for item in value if item)
    else:
        value = str(value).strip()

    if value.startswith('[') and value.endswith(']'):
        # Some clients export list-like strings (for example "['Artist']").
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            pass
        else:
            if isinstance(parsed, (list, tuple)):
                value = ', '.join(str(item).strip() for item in parsed if item)
    return value.strip()

def shorten_udn(udn):
    span = 5
    start = udn.find(':') + 1
    if start != len('uuid:'):
        return udn[:13]

    return udn[start:start+span] + '...' + udn[len(udn)-span:]

# Classes.
class MetaData(namedtuple('MetaData', ['publisher', 'artist', 'title'])):
    def __str__(self):
        return shorten(repr(self), head_len=40, tail_len=40)

class SoapSpacer:
    """Space out SOAP actions."""

    def __init__(self, soap_minimum_interval):
        self.soap_minimum_interval = soap_minimum_interval
        self.next_soap_at = None

    async def wait(self):
        now = time.monotonic()
        if self.next_soap_at is not None:
            interval = self.next_soap_at - now
            if interval > 0:
                await asyncio.sleep(interval)
                now = time.monotonic()
        self.next_soap_at = now + self.soap_minimum_interval

class Renderer:
    """A DLNA MediaRenderer.

    See the Standardized DCP (SDCP) specifications:
      'AVTransport:3 Service'
      'RenderingControl:3 Service'
      'ConnectionManager:3 Service'
    """

    def __init__(self, control_point, upnp_device, renderers_list):
        self.control_point = control_point
        self.upnp_device = upnp_device
        self.renderers_list = renderers_list
        self.root_device = renderers_list.root_device

        self.description = (f'{self.getattr("friendlyName")} - '
                            f'{shorten_udn(upnp_device.UDN)}')
        self.name = self.description
        self.root_device_name = (f'{self.root_device.modelName}-'
                                 f'{self.root_device.udn[-5:]}')
        self.curtask = None             # Renderer.run() task
        self.closing = False
        self.nullsink = None            # NullSink instance
        self.previous_idx = None        # previous sink input index
        self.exit_metadata = None       # sink input meta data at exit
        self.encoder = None
        self.mime_type = None
        self.protocol_info = None
        self.current_uri = None
        self.current_art_uri = ''
        self.artwork_path = None
        self.artwork_mime_type = ''
        self.new_pulse_session = False
        self.suspended_state = False
        self.stream_sessions = StreamSessions(self)
        self.pulse_queue = asyncio.Queue()

    async def close(self):
        if not self.closing:
            self.closing = True
            level = (SYSTEMD_LOG_LEVEL if self.control_point.systemd else
                                                                logging.INFO)
            logger.log(level, f"Closing '{self.name}' renderer")

            # Handle the race condition where the Renderer.run() task
            # has been created but not yet started.
            if self.nullsink is not None:
                for task in self.control_point.cp_tasks:
                    if task.get_name() == self.nullsink.sink.name:
                        while True:
                            if self.curtask is not None:
                                break
                            await asyncio.sleep(0.1)
                        break

            if (self.curtask is not None and
                    asyncio.current_task() != self.curtask):
                self.curtask.cancel()

            await self.stream_sessions.close_session()

            # Close the root device and all of its renderers.
            # The pulse module is unregistered in renderers_list.close() to
            # ensure that it is unregistered before self.root_device is
            # removed from the control_point.root_devices dictionary. This
            # avoids a race condition where the upnp device may be registered
            # again with the same name (i.e. uuid) by pulse following an ssdp
            # discovery (and therefore causing a crash) while the module is
            # still registered.
            await self.renderers_list.close()

    def getattr(self, name):
        """Falling back to root device when upn_device attribute missing."""
        try:
            return getattr(self.upnp_device, name)
        except AttributeError:
            return getattr(self.root_device, name)

    async def disable_root_device(self):
        """Close the renderer and disable its root device."""

        await self.close()
        self.control_point.disable_root_device(self.root_device,
                                               name=self.root_device_name)

    async def pulse_unregister(self):
        if self.nullsink is not None:
            await self.control_point.pulse.unregister(self.nullsink)
            self.nullsink = None

    async def pulse_register(self):
        self.nullsink = await self.control_point.pulse.register(self)
        if self.nullsink is not None:
            return True
        else:
            await self.disable_root_device()
            return False

    def match(self, uri_path):
        return uri_path == f'{AUDIO_URI_PREFIX}/{self.upnp_device.UDN}'

    def match_artwork(self, uri_path):
        return uri_path == f'{ARTWORK_URI_PREFIX}/{self.upnp_device.UDN}'

    def get_artwork_blob(self):
        if not self.artwork_path:
            return None, None
        try:
            with open(self.artwork_path, 'rb') as f:
                data = f.read()
        except OSError:
            return None, None
        content_type = self.artwork_mime_type
        if not content_type:
            content_type = 'application/octet-stream'
        return data, content_type

    def sink_input_art_url(self, sink_input):
        if sink_input is None:
            return ''
        proplist = getattr(sink_input, 'proplist', None)
        if not isinstance(proplist, dict):
            return ''
        for key in ('mpris:artUrl', 'xesam:artUrl', 'media.art.url',
                    'art.url'):
            value = normalize_stream_value(proplist.get(key))
            if value:
                return value
        return ''

    def update_track_artwork(self, sink_input):
        if sink_input is None:
            # Keep last artwork so delayed Chromecast fetches after LOAD do not
            # fail with 404 during track transitions.
            return

        self.current_art_uri = ''
        self.artwork_path = None
        self.artwork_mime_type = ''

        art_url = self.sink_input_art_url(sink_input)
        if not art_url:
            logger.debug(f'No artwork URL available for {self.name}')
            return

        parsed = urllib.parse.urlparse(art_url)
        if parsed.scheme in ('http', 'https'):
            self.current_art_uri = art_url
            logger.debug(f'Using remote artwork URL on {self.name}: {art_url}')
            return

        path = ''
        if parsed.scheme == 'file':
            path = urllib.parse.unquote(parsed.path)
        elif not parsed.scheme:
            path = art_url

        if not path:
            logger.debug(f'Unsupported artwork URL on {self.name}: {art_url!r}')
            return
        if not os.path.isfile(path):
            logger.debug(f'Artwork file is missing on {self.name}: {path!r}')
            return

        self.artwork_path = path
        self.artwork_mime_type = (mimetypes.guess_type(path)[0] or
                                  'application/octet-stream')
        ip_address = getattr(self.root_device, 'local_ipaddress', '127.0.0.1')
        port = getattr(self.control_point, 'port', 8080)
        self.current_art_uri = (f'http://{ip_address}'
                                f':{port}'
                                f'{ARTWORK_URI_PREFIX}/{self.upnp_device.UDN}')
        logger.debug(f'Using proxied artwork URL on {self.name}:'
                     f' {self.current_art_uri}')

    async def add_application(self):
        # Map the application name to the uuid.
        pulse = self.control_point.pulse
        client = await pulse.get_client(self.nullsink.sink_input)
        if client is not None:
            app_name = client.proplist.get('application.name')
            if app_name is not None:
                pulse.add_application(self, app_name, self.upnp_device.UDN)

    async def start_track(self, writer):
        await self.stream_sessions.start_track(writer)

    async def push_second_event_at(self, delay, event, sink, sink_input):
        """Push the first pulse event a second time to start a new session.

        Handle the case where the second pulse event is missing.
        """

        await asyncio.sleep(delay)
        if self.new_pulse_session:
            self.pulse_queue.put_nowait((event, sink, sink_input))

    def log_pulse_event(self, event, sink_input):
        sink_input_index = 'unknown'
        if sink_input is None:
            sink_input = self.nullsink.sink_input
        if sink_input is not None:
            sink_input_index = sink_input.index

        logger.debug(f"'{event}' pulse event [{self.name} "
                     f'sink-input index {sink_input_index}]')

    def sink_input_meta(self, sink_input):
        if sink_input is None:
            return None

        def get_first(proplist, keys):
            for key in keys:
                value = normalize_stream_value(proplist.get(key))
                if value:
                    return value
            return ''

        def get_values(proplist, keys):
            values = []
            for key in keys:
                value = normalize_stream_value(proplist.get(key))
                if value and value not in values:
                    values.append(value)
            return values

        def same(left, right):
            return bool(left and right and left.casefold() == right.casefold())

        def title_from_stream_name(stream_name, publisher):
            if not stream_name:
                return ''

            # Pulse/PipeWire can expose "<app> : <track>" as stream name.
            if publisher:
                match = re.match(
                    rf'^{re.escape(publisher)}\s*:\s*(?P<title>.+)$',
                    stream_name, flags=re.IGNORECASE)
                if match:
                    title = normalize_stream_value(match.group('title'))
                    if title:
                        return title

            if ' : ' in stream_name:
                _, title = stream_name.split(' : ', 1)
                title = normalize_stream_value(title)
                if title:
                    return title

            return stream_name

        proplist = sink_input.proplist
        publisher = get_first(proplist, ('application.name',
                                         'application.process.binary'))
        artist = get_first(proplist, ('media.artist', 'xesam:artist'))
        stream_name = normalize_stream_value(getattr(sink_input, 'name', None))
        stream_title = title_from_stream_name(stream_name, publisher)

        title = ''
        title_candidates = get_values(proplist, ('media.title', 'media.name',
                                                 'xesam:title',
                                                 'window.title',
                                                 'node.description',
                                                 'node.nick'))
        if title_candidates:
            # Prefer a value that is not just the app name.
            for value in title_candidates:
                if not same(value, publisher):
                    title = value
                    break
            if not title:
                title = title_candidates[0]

        if stream_title and (not title or same(title, publisher)):
            title = stream_title

        if not self.encoder.track_metadata:
            title = publisher
            artist = ''

        return MetaData(publisher, artist, title)

    async def handle_action(self, action):
        """An action is either 'Stop' or an instance of MetaData.

        DLNA TransportStates are 'NO_MEDIA_PRESENT', 'STOPPED' or
        'PLAYING', the other states are silently ignored.
        """

        # Get the stream state.
        state = await self.get_transport_state()

        # Space out SOAP actions that start or stop a stream.
        if self.encoder.soap_minimum_interval != 0:
            await self.soap_spacer.wait()

        # Run an AVTransport action if needed.
        try:
            if state not in ('STOPPED', 'NO_MEDIA_PRESENT'):
                if isinstance(action, MetaData):
                    if self.encoder.track_metadata:
                        await self.set_nextavtransporturi(action, state)
                    return
                elif action == 'Stop':
                    index = self.get_sink_input_index()
                    self.stream_sessions.stream_tasks.create_task(
                                            self.maybe_stop(index, state),
                                            name=f'maybe_stop {self.name}')
                    return
            elif isinstance(action, MetaData):
                await self.set_avtransporturi(action, state)
                log_action(self.name, 'Play', state)
                await self.play()
                return
        except UPnPSoapFaultError as e:
            arg = e.args[0]
            if (hasattr(arg, 'errorCode') and
                    arg.errorCode in IGNORED_SOAPFAULTS):
                error_msg = IGNORED_SOAPFAULTS[arg.errorCode]
                logger.warning(f"Ignoring SOAP error '{error_msg}'")
            else:
                raise

        log_action(self.name, action, state, ignored=True)

    async def handle_pulse_event(self):
        """Handle a PulseAudio event.

        An event is either 'new', 'change', 'remove' or 'exit'.
        An action is either 'Stop' or an instance of MetaData.
        """

        if self.nullsink is None:
            # The Renderer instance is now temporarily disabled.
            return

        event, sink, sink_input = await self.pulse_queue.get()
        self.log_pulse_event(event, sink_input)

        # Note that, at each pulseaudio event, a new instance of sink and
        # sink_input is generated by the libpulse library.
        if (sink_input is not None and
                self.nullsink.sink_input is not None  and
                sink_input.index != self.nullsink.sink_input.index):
            self.previous_idx = self.nullsink.sink_input.index

        # Process the event and set the new attributes values of nullsink.
        if event in ('remove', 'exit'):
            if self.nullsink.sink_input is not None:
                self.update_track_artwork(None)
                if event == 'exit':
                    self.exit_metadata = self.sink_input_meta(
                                                    self.nullsink.sink_input)
                await self.handle_action('Stop')
            return

        assert sink is not None and sink_input is not None
        try:
            cur_metadata = self.sink_input_meta(sink_input)
            if self.nullsink.sink_input is None:
                self.new_pulse_session = True
                # The reconnection of a sink-input after an exit is signaled
                # by only one 'change' event, while a new session is signaled
                # by two events in both PulseAudio and Pipewire, the track
                # meta data (if any) being only available in the second one.
                #
                # push_second_event_at() handles the case where the second
                # event is missing after a NEW_SESSION_MAX_DELAY delay.
                if cur_metadata == self.exit_metadata:
                    self.exit_metadata = None
                else:
                    self.control_point.cp_tasks.create_task(
                        self.push_second_event_at(NEW_SESSION_MAX_DELAY,
                                                  event, sink, sink_input),
                        name='new_session_max_delay')
                    return

            if self.new_pulse_session:
                self.new_pulse_session = False
                # So that the device may display at least some useful info.
                if not cur_metadata.title:
                    cur_metadata = cur_metadata._replace(
                                            title=cur_metadata.publisher)
                self.update_track_artwork(sink_input)
                await self.handle_action(cur_metadata)

            # A new track.
            else:
                prev_metadata = self.sink_input_meta(self.nullsink.sink_input)
                # Note that if self.encoder.track_metadata is false, then
                # cur_metadata.title == prev_metadata.title since 'title'
                # value is 'publisher' value.
                if (prev_metadata is not None and
                        cur_metadata.title and
                        cur_metadata != prev_metadata):
                    self.update_track_artwork(sink_input)
                    await self.handle_action(cur_metadata)

        finally:
            # If the Renderer instance is not temporarily disabled.
            if self.nullsink is not None:
                self.nullsink.sink = sink
                self.nullsink.sink_input = sink_input

    async def soap_action(self, serviceId, action, args={}):
        """Send a SOAP action.

        Return the dict {argumentName: out arg value} if successfull,
        otherwise an instance of the upnp.xml.SoapFault namedtuple defined by
        field names in ('errorCode', 'errorDescription').
        """

        if self.upnp_device.closed:
            raise UPnPSoapFaultError('UPnPRootDevice is closed')

        service = self.upnp_device.serviceList[serviceId]
        return await service.soap_action(action, args, log_debug=False)

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
        self.soap_spacer = SoapSpacer(self.encoder.soap_minimum_interval)
        return True

    def didl_lite_metadata(self, metadata):
        """Build de didl-lite xml string.

        The returned string is built with ../tools/build_didl_lite.py.
        """

        album_art = ''
        if self.current_art_uri:
            album_art = ('<upnp:albumArtURI>'
                         f'{xml_escape(self.current_art_uri)}'
                         '</upnp:albumArtURI>')

        didl_lite = (
          f'''
        <DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
          xmlns:dc="http://purl.org/dc/elements/1.1/"
          xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
        <item id="0" parentID="0" restricted="0">
          <dc:title>{xml_escape(metadata.title)}</dc:title>
          <upnp:class>object.item.audioItem.musicTrack</upnp:class>
          <dc:publisher>{xml_escape(metadata.publisher)}</dc:publisher>
          <upnp:artist>{xml_escape(metadata.artist)}</upnp:artist>
          {album_art}
          <res protocolInfo="{self.protocol_info}">
            {self.current_uri}</res>
        </item></DIDL-Lite>
          '''
        )
        return normalize_xml(didl_lite)

    async def set_avtransporturi(self, metadata, state):
        await self.add_application()

        action = 'SetAVTransportURI'
        didl_lite = self.didl_lite_metadata(metadata)
        args = {'InstanceID': 0,
                'CurrentURI': self.current_uri,
                'CurrentURIMetaData': didl_lite
                }
        log_action(self.name, action, state, msg=didl_lite)
        logger.info(f'{metadata}'
                    f'{NL_INDENT}URL: {self.current_uri}')
        await self.soap_action(AVTRANSPORT, action, args)

    async def set_nextavtransporturi(self, metadata, state):
        action = 'SetNextAVTransportURI'
        didl_lite = self.didl_lite_metadata(metadata)
        args = {'InstanceID': 0,
                'NextURI': self.current_uri,
                'NextURIMetaData': didl_lite
                }

        await self.stream_sessions.stop_track()
        log_action(self.name, action, state, msg=didl_lite)
        logger.info(f'{metadata}')
        logger.debug(f'URL: {self.current_uri}')
        await self.soap_action(AVTRANSPORT, action, args)

    async def get_transport_state(self):
        res = await self.soap_action(AVTRANSPORT, 'GetTransportInfo',
                                     {'InstanceID': 0})
        state = res['CurrentTransportState']
        return state

    async def play(self, speed=1):
        args = {'InstanceID': 0}
        args['Speed'] = speed
        await self.soap_action(AVTRANSPORT, 'Play', args)

    async def stop(self):
        args = {'InstanceID': 0}
        await self.soap_action(AVTRANSPORT, 'Stop', args)

    def get_sink_input_index(self):
        if (self.nullsink is not None and
                    self.nullsink.sink_input is not None):
            return self.nullsink.sink_input.index

    @log_unhandled_exception(logger)
    async def maybe_stop(self, index, state):
        # Work around for issue #48:
        #   KDE music players trigger (randomly) 'remove' events upon track
        #   changes.
        await asyncio.sleep(ISSUE_48_TIMER)

        cur_index = self.get_sink_input_index()
        if cur_index is not None and cur_index != index:
            # A new track is being played.
            # Ignore the 'remove' event.
            self.log_pulse_event('remove ignored', self.nullsink.sink_input)
            return

        self.nullsink.sink_input = None

        # Let the HTTP 1.1 chunked transfer encoding handles the closing of
        # the stream.
        log_action(self.name, 'Closing-Stop', state)
        await self.stream_sessions.close_session()

        # Not really necessary.
        log_action(self.name, 'Stop', state)
        try:
            await self.stop()
        except UPnPSoapFaultError:
            pass

    def set_current_uri(self):
        self.current_uri = (f'http://{self.root_device.local_ipaddress}'
                            f':{self.control_point.port}'
                            f'{AUDIO_URI_PREFIX}/{self.upnp_device.UDN}')

    @log_unhandled_exception(logger)
    async def run(self):
        """Run the Renderer task."""

        self.curtask = asyncio.current_task()
        try:
            if not await self.select_encoder(self.upnp_device.UDN):
                return
            self.set_current_uri()
            level = (SYSTEMD_LOG_LEVEL if self.control_point.systemd else
                                                                logging.INFO)
            logger.log(level, f"New '{self.name}' renderer with {self.encoder}"
                              f" handling '{self.mime_type}'")
            logger.info(f'{NL_INDENT}URL: {self.current_uri}')

            # Handle the case where pa-dlna is started after streaming has
            # started (no pulse event).
            pulse = self.control_point.pulse
            sink_input = await pulse.get_sink_input(self)

            # Work around the wireplumber bug (issue #15).
            if sink_input is None:
                sink_input = await pulse.find_sink_input(self.upnp_device.UDN)
                if sink_input is not None:
                    # 'sink_input' is None if move_sink_input() fails.
                    sink_input = await pulse.move_sink_input(
                                            sink_input, self.nullsink.sink)

            if sink_input is not None:
                logger.info(f"Streaming '{sink_input.name}' on {self.name}")
                self.nullsink.sink_input = sink_input
                cur_metadata = self.sink_input_meta(sink_input)
                if not cur_metadata.title:
                    cur_metadata = cur_metadata._replace(
                                            title=cur_metadata.publisher)
                self.update_track_artwork(sink_input)
                # Trigger 'SetAVTransportURI' and 'Play' soap actions.
                await self.handle_action(cur_metadata)

            # If running as a test of test_pa_dlna.py, break from the loop
            # when the 'test_end' asyncio future is done. Otherwise run for
            # ever.
            test_end = self.control_point.test_end
            while test_end is None or not test_end.done():
                await self.handle_pulse_event()

        except asyncio.CancelledError:
            pass
        except UPnPSoapFaultError as e:
            logger.error(f'{e!r}')
        except (OSError, UPnPClosedDeviceError, ControlPointAbortError) as e:
            logger.error(f'{e!r}')
        except Exception:
            await self.disable_root_device()
            raise
        finally:
            await self.close()


class CastRenderer(Renderer):
    """A Chromecast renderer."""

    def __init__(self, control_point, upnp_device, renderers_list):
        super().__init__(control_point, upnp_device, renderers_list)
        self.description = (f'Chromecast: {self.getattr("friendlyName")} - '
                            f'{shorten_udn(upnp_device.UDN)}')
        self.name = self.description
        self._encoder_candidates = []
        self._encoder_idx = 0
        self._state = 'STOPPED'

    def _set_encoder_candidate(self, idx):
        encoder, mime_type, protocol_info = self._encoder_candidates[idx]
        self._encoder_idx = idx
        self.encoder = encoder
        self.mime_type = mime_type
        self.protocol_info = protocol_info
        self.soap_spacer = SoapSpacer(self.encoder.soap_minimum_interval)

    async def _run_cast(self, func):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func)

    async def _media_controller(self):
        provider = self.control_point.cast_provider
        if provider is None:
            raise RuntimeError('Missing Chromecast provider')
        cast = await provider.get_chromecast(self.root_device)
        media_controller = getattr(cast, 'media_controller', None)
        if media_controller is None:
            raise RuntimeError('Missing Chromecast media controller')
        return media_controller

    def _build_encoder_candidates(self, udn):
        protocol_infos = [f'http-get:*:{mtype}:*' for mtype in
                          CHROMECAST_MIME_TYPES]
        remaining = list(protocol_infos)
        candidates = []
        seen = set()
        while remaining:
            pinfo = {'Sink': ','.join(remaining)}
            result = select_encoder(self.control_point.config, self.name,
                                    pinfo, udn)
            if result is None:
                break
            encoder, mime_type, protocol_info = result
            key = (id(encoder), mime_type.lower())
            if key in seen:
                break
            seen.add(key)
            candidates.append((encoder, mime_type, protocol_info))
            remaining = [p for p in remaining if p != protocol_info]
        return candidates

    async def select_encoder(self, udn):
        self._encoder_candidates = self._build_encoder_candidates(udn)
        if not self._encoder_candidates:
            logger.error(f'Cannot find an encoder matching the {self.name}'
                         f' supported mime types')
            await self.disable_root_device()
            return False

        self._set_encoder_candidate(0)
        return True

    async def handle_action(self, action):
        """Handle pulse actions for Chromecast without a second play call.

        play_media() already starts playback, so calling media_controller.play()
        afterwards can fail on some devices.
        """

        state = await self.get_transport_state()

        if self.encoder.soap_minimum_interval != 0:
            await self.soap_spacer.wait()

        if state not in ('STOPPED', 'NO_MEDIA_PRESENT'):
            if isinstance(action, MetaData):
                if self.encoder.track_metadata:
                    await self.set_nextavtransporturi(action, state)
                return
            elif action == 'Stop':
                index = self.get_sink_input_index()
                self.stream_sessions.stream_tasks.create_task(
                                        self.maybe_stop(index, state),
                                        name=f'maybe_stop {self.name}')
                return
        elif isinstance(action, MetaData):
            await self.set_avtransporturi(action, state)
            return

        log_action(self.name, action, state, ignored=True)

    async def _load_media_once(self, metadata):
        media_controller = await self._media_controller()
        cast = getattr(self.root_device, 'cast', None)
        if cast is None and self.control_point.cast_provider is not None:
            cast = await self.control_point.cast_provider.get_chromecast(
                self.root_device)

        def play_media():
            if cast is not None:
                desired_app_id = getattr(media_controller, 'supporting_app_id',
                                         None)
                if desired_app_id:
                    current_app_id = getattr(getattr(cast, 'status', None),
                                             'app_id', None)
                    logger.info(f'Ensure Chromecast app on {self.name}:'
                                f' {current_app_id!r} -> {desired_app_id!r}')
                    # Some Cast built-in devices keep media UI in background
                    # unless we explicitly restart the receiver app.
                    if (current_app_id == desired_app_id and
                            hasattr(cast, 'quit_app')):
                        try:
                            cast.quit_app(timeout=3)
                        except Exception as e:
                            logger.debug(f'Ignoring quit_app() on {self.name}:'
                                         f' {e!r}')
                    cast.start_app(desired_app_id, force_launch=True, timeout=5)
            media_metadata = {
                'metadataType': 3,  # MusicTrackMediaMetadata
                'title': metadata.title
            }
            if metadata.artist:
                media_metadata['artist'] = metadata.artist
            if metadata.publisher:
                media_metadata['albumName'] = metadata.publisher
            kwargs = {
                'stream_type': 'LIVE',
                'metadata': media_metadata
            }
            media_controller.play_media(self.current_uri, self.mime_type,
                                        **kwargs)
            if hasattr(media_controller, 'block_until_active'):
                media_controller.block_until_active(5)

        await self._run_cast(play_media)
        self._state = 'PLAYING'

    async def _load_media(self, metadata, state, action):
        err = None
        for idx in range(self._encoder_idx, len(self._encoder_candidates)):
            if idx != self._encoder_idx:
                self._set_encoder_candidate(idx)
                logger.warning(f'Retry {action} on {self.name} with'
                               f' {self.encoder} handling'
                               f" '{self.mime_type}'")
            try:
                await self._load_media_once(metadata)
                logger.info(f'{metadata}'
                            f'{NL_INDENT}URL: {self.current_uri}')
                log_action(self.name, action, state)
                return
            except Exception as e:
                err = e
                logger.warning(f'Chromecast playback failed on {self.name}'
                               f' with {self.encoder} handling'
                               f" '{self.mime_type}': {e!r}")

        if err is not None:
            raise err

    async def set_avtransporturi(self, metadata, state):
        await self.add_application()
        await self._load_media(metadata, state, 'SetAVTransportURI')

    async def set_nextavtransporturi(self, metadata, state):
        await self.stream_sessions.stop_track()
        await self._load_media(metadata, state, 'SetNextAVTransportURI')

    async def get_transport_state(self):
        try:
            media_controller = await self._media_controller()
            player_state = getattr(media_controller.status, 'player_state',
                                   None)
        except Exception:
            return 'STOPPED'

        if player_state in ('PLAYING', 'BUFFERING'):
            return 'PLAYING'
        if player_state in ('STOPPED', 'PAUSED'):
            return 'STOPPED'
        if player_state in ('IDLE', None):
            return 'NO_MEDIA_PRESENT'
        return 'STOPPED'

    async def play(self, speed=1):
        media_controller = await self._media_controller()

        def play():
            media_controller.play()

        await self._run_cast(play)
        self._state = 'PLAYING'

    async def stop(self):
        media_controller = await self._media_controller()

        def stop():
            media_controller.stop()

        await self._run_cast(stop)
        self._state = 'STOPPED'


class DLNATestDevice(Renderer):
    """Non UPnP Renderer to be used for testing."""

    class RootDevice:

        LOOPBACK = '127.0.0.1'

        def __init__(self, mime_type, control_point):
            self.control_point = control_point
            # Needed by soap_action() in the test suite.
            self.mime_type = mime_type
            self.peer_ipaddress = self.LOOPBACK
            self.local_ipaddress = self.LOOPBACK

            match = re.match(r'audio/([^;]+)', mime_type)
            name = match.group(1)
            self.modelName = f'DLNATest_{name}'
            self.friendlyName = self.modelName
            self.UDN = get_udn(name.encode())
            self.udn = self.UDN

        def close(self):
            logger.info(f"Close '{self.modelName}' root device")

    def __init__(self, control_point, mime_type):
        root_device = self.RootDevice(mime_type, control_point)
        renderers_list = RenderersList(control_point, root_device)
        renderers_list.append(self)

        super().__init__(control_point, root_device, renderers_list)
        control_point.root_devices[root_device] = renderers_list
        self.mime_type = mime_type

    async def play(self, speed=1):
        pass

    async def soap_action(self, serviceId, action, args='unused'):
        if action == 'GetProtocolInfo':
            # Use the 'mime_type' attribute of the root device instead of the
            # renderer as expected since this method  is also used by tests at
            # pa_dlna.tests.test_pa_dlna.PatchGetNotificationTests.
            return {'Source': None,
                    'Sink': f'http-get:*:{self.root_device.mime_type}:*'
                    }
        elif action == 'GetTransportInfo':
            state = ('PLAYING' if self.stream_sessions.is_playing else
                     'STOPPED')
            return {'CurrentTransportState': state}

class RenderersList(UserList):
    """The list of all Renderers of a root device as a dict.

    This includes the root device if it is a MediaRenderer and all embedded
    devices that are MediaRenderer.
    """

    def __init__(self, control_point, root_device):
        super().__init__()
        self.control_point = control_point
        self.root_device = root_device
        self.closed = False

    def build_list(self):
        # Build the list of renderers.
        for upnp_device in UPnPDevice.embedded_devices_generator(
                                                            self.root_device):
            if re.match(rf'{MEDIARENDERER}(\d+)', upnp_device.deviceType):
                self.data.append(Renderer(self.control_point, upnp_device,
                                          self))

    async def close(self):
        if not self.closed:
            self.closed = True
            for renderer in self.data:
                try:
                    await renderer.close()
                    await renderer.pulse_unregister()
                except Exception as e:
                    logger.error(f'Got exception closing {renderer.name}:'
                                 f' {e!r}')

            if self.root_device in self.control_point.root_devices:
                del self.control_point.root_devices[self.root_device]
            self.root_device.close()


class CastRenderersList(RenderersList):
    """The list of Chromecast renderers of a root device."""

    def build_list(self):
        self.data.append(CastRenderer(self.control_point, self.root_device,
                                      self))


class AVControlPoint(UPnPApplication):
    """Control point with Content.

    Manage PulseAudio and the DLNA/Chromecast renderer devices.
    See section 6.6 of "UPnP AV Architecture:2".
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closing = False
        self.root_devices = {}      # dictionary {root_device: renderers_list}
        self.curtask = None         # task running run_control_point()
        self.pulse = None           # Pulse instance
        self.start_event = None
        self.upnp_control_point = None
        self.cast_provider = None
        self.http_servers = {}          # {IPv4 address: http server instance}
        self.register_sem = asyncio.Semaphore()
        self.cp_tasks = AsyncioTasks()

        # 'test_end' is meant to be used as an asyncio future by tests in
        # test_pa_dlna.py.
        self.test_end = None

    @log_unhandled_exception(logger)
    async def shutdown(self, end_event):
        try:
            await end_event.wait()
            await self.close('Got SIGINT or SIGTERM')
        finally:
            loop = asyncio.get_running_loop()
            for sig in (SIGINT, SIGTERM):
                loop.remove_signal_handler(sig)

    @log_unhandled_exception(logger)
    async def close(self, msg=None):
        # This coroutine may be run as a task by AVControlPoint.abort().
        if not self.closing:
            self.closing = True

            if self.cast_provider is not None:
                await self.cast_provider.close()
                self.cast_provider = None

            # The semaphore prevents a race condition where a new Renderer
            # is awaiting the registration of a sink with pulseaudio while
            # the renderers are being closed here. In that case,
            # this sink would never be unregistered.
            async with self.register_sem:
                for renderers_list in list(self.root_devices.values()):
                    await renderers_list.close()

            if self.pulse is not None:
                await self.pulse.close()

            if self.curtask != asyncio.current_task():
                if sys.version_info[:2] >= (3, 9):
                    self.curtask.cancel(msg)
                else:
                    self.curtask.cancel()

    def abort(self, msg):
        """Abort the whole program from a non-main task."""

        # Should be called instead of 'assert False' when there is a
        # programatic error.
        self.cp_tasks.create_task(self.close(msg), name='abort')
        raise ControlPointAbortError(msg)

    def disable_root_device(self, root_device, name=None):
        if getattr(root_device, 'source', None) == 'chromecast':
            if self.cast_provider is not None:
                self.cast_provider.disable_root_device(root_device, name=name)
        else:
            self.upnp_control_point.disable_root_device(root_device, name=name)

    async def register(self, renderer):
        """Load the null-sink module.

        If successfull, create the http_server if needed and create the
        renderer task.
        """

        async with self.register_sem:
            if self.closing:
                return
            registered = await renderer.pulse_register()

        if registered:
            root_device = renderer.root_device
            http_server = await self.create_httpserver(
                                                root_device.local_ipaddress)
            http_server.allow_from(root_device.peer_ipaddress)

            # Create the renderer task.
            self.cp_tasks.create_task(renderer.run(),
                                      name=renderer.nullsink.sink.name)

    async def create_httpserver(self, ip_address):
        """Create the http_server task."""

        if ip_address not in self.http_servers:
            http_server = HTTPServer(self, ip_address, self.port)
            self.cp_tasks.create_task(http_server.run(),
                                      name=f'http_server-{ip_address}')
            await http_server.startup
            self.http_servers[ip_address] = http_server
        return self.http_servers[ip_address]

    def renderers(self):
        """Generator yielding all the renderers."""

        for renderers_list in self.root_devices.values():
            for renderer in renderers_list:
                yield renderer

    async def handle_upnp_notifications(self):
        while True:
            notif, root_device = await (
                                self.upnp_control_point.get_notification())
            if (notif, root_device) == QUEUE_CLOSED:
                logger.debug('UPnP queue is closed')
                return
            logger.info(f"Got '{notif}' notification for {root_device}")

            if (not hasattr(root_device, 'deviceType') or
                    not hasattr(root_device, 'modelName')):
                logger.info(f"Ignore '{root_device}': "
                            f'missing deviceType or modelName')
                self.disable_root_device(root_device)
                continue

            renderers_list = RenderersList(self, root_device)
            renderers_list.build_list()
            if not renderers_list:
                if not self.upnp_control_point.is_disabled(root_device):
                    logger.info(f"Ignore '{root_device}':"
                                f' no MediaRenderer found')
                    self.disable_root_device(root_device)
                continue

            is_new_renderer_list = root_device not in self.root_devices
            if notif == 'alive':
                if self.upnp_control_point.is_disabled(root_device):
                    logger.debug(f'Ignore disabled {root_device}')
                elif is_new_renderer_list:
                    if root_device.local_ipaddress is not None:
                        self.root_devices[root_device] = renderers_list
                        for renderer in renderers_list:
                            await self.register(renderer)
            elif notif == 'byebye':
                if not is_new_renderer_list:
                    # Close the renderers_list.
                    await self.root_devices[root_device].close()
            else:
                raise RuntimeError('Error: Unknown notification')

    async def handle_cast_notifications(self):
        while True:
            notif, root_device = await self.cast_provider.get_notification()
            if (notif, root_device) == QUEUE_CLOSED:
                logger.debug('Chromecast queue is closed')
                return

            logger.info(f"Got '{notif}' notification for {root_device}")

            renderers_list = CastRenderersList(self, root_device)
            renderers_list.build_list()
            is_new_renderer_list = root_device not in self.root_devices
            if notif == 'alive':
                if self.cast_provider.is_disabled(root_device):
                    logger.debug(f'Ignore disabled {root_device}')
                elif is_new_renderer_list:
                    if root_device.local_ipaddress is not None:
                        self.root_devices[root_device] = renderers_list
                        for renderer in renderers_list:
                            await self.register(renderer)
            elif notif == 'byebye':
                if not is_new_renderer_list:
                    await self.root_devices[root_device].close()
            else:
                raise RuntimeError('Error: Unknown notification')

    @log_unhandled_exception(logger)
    async def run_control_point(self):
        try:
            self.curtask = asyncio.current_task()
            self.start_event = asyncio.Event()

            if not self.config.any_available():
                raise RuntimeError('Error: No encoder is available')

            parec_pgm = shutil.which('parec')
            if parec_pgm is None:
                raise RuntimeError("Error: The pulseaudio 'parec'"
                                   ' program cannot be found')
            self.parec_cmd = [parec_pgm]

            # Add the signal handlers.
            end_event = asyncio.Event()
            self.cp_tasks.create_task(self.shutdown(end_event),
                                      name='shutdown')
            loop = asyncio.get_running_loop()
            for sig in (SIGINT, SIGTERM):
                loop.add_signal_handler(sig, end_event.set)

            # Run the UPnP control point.
            with UPnPControlPoint(
                    self.ip_addresses, self.nics, self.msearch_interval,
                    self.msearch_port, self.ttl) as self.upnp_control_point:
                # Create the Pulse task.
                self.pulse = Pulse(self)
                self.cp_tasks.create_task(self.pulse.run(), name='pulse')

                # Wait for the connection to PulseAudio to be ready.
                await self.start_event.wait()

                # Register the DLNATestDevices.
                for mtype in self.test_devices:
                    rndr = DLNATestDevice(self, mtype)
                    await self.register(rndr)

                self.cast_provider = ChromecastProvider(self)
                if await self.cast_provider.start():
                    self.cp_tasks.create_task(self.handle_cast_notifications(),
                                              name='chromecast')

                # Handle UPnP notifications for ever.
                await self.handle_upnp_notifications()

        except asyncio.CancelledError as e:
            level = SYSTEMD_LOG_LEVEL if self.systemd else logging.INFO
            logger.log(level, f'Main task got: {e!r}')
        finally:
            await self.close()

    def __str__(self):
        return 'pa-dlna'

# The main function.
def main():
    padlna_main(AVControlPoint, __doc__)

if __name__ == '__main__':
    padlna_main(AVControlPoint, __doc__)
