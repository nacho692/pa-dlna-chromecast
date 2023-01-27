"""An UPnP control point forwarding PulseAudio streams to DLNA devices."""

import sys
import shutil
import asyncio
import logging
import re
import hashlib
from ipaddress import IPv4Interface, IPv4Address
from signal import SIGINT, SIGTERM
from collections import namedtuple

from .init import padlna_main, UPnPApplication, ControlPointAbortError
from .pulseaudio import Pulse
from .http_server import StreamSessions, HTTPServer
from .encoders import select_encoder
from .upnp import (UPnPControlPoint, UPnPClosedDeviceError,
                   UPnPSoapFaultError, ipv4_addresses, NL_INDENT, shorten,
                   log_exception, AsyncioTasks)

logger = logging.getLogger('pa-dlna')

AUDIO_URI_PREFIX = '/audio-content'
MEDIARENDERER = 'urn:schemas-upnp-org:device:MediaRenderer:'
AVTRANSPORT = 'urn:upnp-org:serviceId:AVTransport'
RENDERINGCONTROL = 'urn:upnp-org:serviceId:RenderingControl'
CONNECTIONMANAGER = 'urn:upnp-org:serviceId:ConnectionManager'
IGNORED_SOAPFAULTS = {'701': 'Transition not available',
                      '715': "Content 'BUSY'"}

UPnPAction = namedtuple('UPnPAction', ['action', 'state'])
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

# Classes.
class MetaData(namedtuple('MetaData', ['publisher', 'artist', 'title'])):
    def __str__(self):
        return shorten(repr(self), head_len=40, tail_len=40)

class Renderer:
    """A DLNA MediaRenderer.

    See the Standardized DCP (SDCP) specifications:
      'AVTransport:3 Service'
      'RenderingControl:3 Service'
      'ConnectionManager:3 Service'
    """

    def __init__(self, control_point, local_ipaddress, root_device):
        self.control_point = control_point
        self.local_ipaddress = local_ipaddress
        self.root_device = root_device
        udn_tail = root_device.udn[-5:]
        self.name = f'{root_device.modelName}-{udn_tail}'
        self.description = f'{root_device.friendlyName} - {udn_tail}'
        self.curtask = None             # Renderer.run() task
        self.closing = False
        self.nullsink = None            # NullSink instance
        self.previous_idx = None        # index of previous sink input
        self.encoder = None
        self.mime_type = None
        self.protocol_info = None
        self.current_uri = None
        self.new_pulse_session = False
        self.stream_sessions = StreamSessions(self)
        self.pulse_queue = asyncio.Queue()

    async def close(self):
        if not self.closing:
            self.closing = True
            logger.info(f'Close {self.name} renderer')
            if (self.curtask is not None and
                    asyncio.current_task() != self.curtask):
                self.curtask.cancel()
            await self.pulse_unregister()
            await self.stream_sessions.close_session()

            # Closing the root device will trigger a 'byebye' notification and
            # the renderer will be removed from self.control_point.renderers.
            self.root_device.close()

    async def disable_for(self, *, period):
        """Disable the renderer for 'period' seconds."""

        # Unload the null-sink module, sleep 'period' seconds and load a new
        # module. During  the sleep period, the stream that was routed to this
        # null-sink is routed to the default sink instead of being silently
        # discarded by the null-sink. After loading the new null-sink module,
        # the renderer receives a 'change' pulse event and starts a new stream
        # session.
        await self.pulse_unregister()

        if period:
            logger.info(f'Wait {period} seconds before'
                        f' re-enabling {self.name}')
            await asyncio.sleep(period)

        if not await self.pulse_register():
            logger.error(f'Cannot load new null-sink module for {self.name}')
            await self.close()

    async def disable_root_device(self):
        """Close the renderer and disable its root device."""

        await self.close()
        self.control_point.disable_root_device(self.root_device,
                                               name=self.name)

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
        return uri_path == f'{AUDIO_URI_PREFIX}/{self.root_device.udn}'

    async def start_track(self, writer):
        await self.stream_sessions.start_track(writer)

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

        if not self.encoder.track_metadata:
            title = publisher
            artist = ''

        return MetaData(publisher, artist, title)

    async def handle_action(self, action):
        """ An action is either 'Stop', 'Pause' or an instance of MetaData.

        This method is run by the 'pulse' task.
        """

        # Get the stream state.
        timeout = 1.0
        try:
            state = await asyncio.wait_for(self.get_transport_state(),
                                           timeout=timeout)
        except asyncio.TimeoutError:
            state = ('PLAYING' if self.stream_sessions.is_playing else
                     'STOPPED')
            logger.debug(f'{self.name} stream state: {state} '
                         f'(GetTransportInfo timed out after {timeout}'
                         f' second)')

        # Run an AVTransport action if needed.
        try:
            if state not in ('STOPPED', 'NO_MEDIA_PRESENT'):
                if (self.encoder.track_metadata and
                        isinstance(action, MetaData)):
                    await self.set_nextavtransporturi(self.name,
                                                      action, state)
                    return
                elif action == 'Stop':
                    # Do not use the corresponding soap action. Let the
                    # HTTP 1.1 chunked transfer encoding handles the closing
                    # of the stream.
                    log_action(self.name, action, state)
                    await self.stream_sessions.close_session()
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
                    log_action(self.name, 'Play', state)
                    await self.play()
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
        """Handle a PulseAudio event."""

        # The 'sink' and 'sink_input' variables define the new state.
        # 'self.nullsink' holds the state prior to this event.
        event, sink, sink_input = await self.pulse_queue.get()
        if self.nullsink is None:
            # The Renderer instance is now temporarily disabled.
            return

        prev_state, new_state = self.pulse_states(sink)
        self.log_pulse_event(event, prev_state, new_state, sink_input)

        # Note that, at each pulseaudio event, a new instance of sink and
        # sink_input is generated by the pulsectl library.
        #
        # Ignore pulse events from a previous sink input.
        # These events are generated by pulseaudio after the user starts a new
        # track.
        if sink_input is not None:
            if sink_input.index == self.previous_idx:
                logger.debug(f"'{event}' ignored pulseaudio event related to"
                             f' previous sink-input of {self.name}')
                return

            if (self.nullsink.sink_input is not None  and
                    sink_input.index != self.nullsink.sink_input.index):
                self.previous_idx = self.nullsink.sink_input.index

        # Process the event and set the new attributes values of nullsink.
        if event in ('remove', 'exit'):
            self.nullsink.sink_input = None
            await self.handle_action('Stop')
            return

        assert sink is not None and sink_input is not None
        cur_metadata = self.sink_input_meta(sink_input)

        # A new pulse session.
        if (self.nullsink.sink_input is None or
                (event == 'new' and new_state == 'idle')):
            self.new_pulse_session = True

        # This will trigger 'SetAVTransportURI' and 'Play' soap
        # actions if the device is in the appropriate state.
        if self.new_pulse_session:
            if new_state == 'running':
                # So that the device may display at least some useful info.
                if not cur_metadata.title:
                    cur_metadata = cur_metadata._replace(
                                            title=cur_metadata.publisher)
                self.new_pulse_session = False
                await self.handle_action(cur_metadata)

        # This will trigger a 'SetNextAVTransportURI' soap action if the
        # device is in the appropriate state.
        elif (cur_metadata.title != '' and
                (prev_state, new_state) == ('running', 'running')):
            prev_metadata = self.sink_input_meta(self.nullsink.sink_input)
            if cur_metadata is not None and cur_metadata != prev_metadata:
                await self.handle_action(cur_metadata)

        elif (prev_state, new_state) == ('running', 'idle'):
            await self.handle_action('Pause')

        if self.nullsink is None:
            # The Renderer instance is now temporarily disabled.
            return
        self.nullsink.sink = sink
        self.nullsink.sink_input = sink_input

    async def soap_action(self, serviceId, action, args={}):
        """Send a SOAP action.

        Return the dict {argumentName: out arg value} if successfull,
        otherwise an instance of the upnp.xml.SoapFault namedtuple defined by
        field names in ('errorCode', 'errorDescription').
        """

        service = self.root_device.serviceList[serviceId]
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

    async def set_nextavtransporturi(self, name, metadata, state):
        action = 'SetNextAVTransportURI'
        didl_lite_metadata = self.didl_lite_metadata(metadata)
        args = {'InstanceID': 0,
                'NextURI': self.current_uri,
                'NextURIMetaData': didl_lite_metadata
                }

        await self.stream_sessions.stop_track()
        log_action(name, action, state, msg=didl_lite_metadata)
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

    @log_exception(logger)
    async def run(self):
        """Run the Renderer task."""

        self.curtask = asyncio.current_task()
        try:
            udn = self.root_device.udn
            if not await self.select_encoder(udn):
                return
            self.current_uri = (f'http://{self.local_ipaddress}'
                                f':{self.control_point.port}'
                                f'{AUDIO_URI_PREFIX}/{udn}')
            logger.info(f'New {self.name} renderer with {self.encoder}'
                        f" handling '{self.mime_type}'"
                        f'{NL_INDENT}URL: {self.current_uri}')

            while True:
                await self.handle_pulse_event()

        except asyncio.CancelledError:
            await self.close()
        except (OSError, UPnPSoapFaultError, UPnPClosedDeviceError) as e:
            logger.error(f'{e!r}')
            await self.close()
        except ControlPointAbortError as e:
            logger.error(f'{e!r}')
        except Exception as e:
            logger.exception(f'{e!r}')
            await self.disable_root_device()

class DLNATestDevice(Renderer):
    """Non UPnP Renderer to be used for testing."""

    class RootDevice:

        LOOPBACK = '127.0.0.1'

        def __init__(self, renderer, mime_type, control_point):
            self.control_point = control_point
            self.renderer = renderer
            self.peer_ipaddress = self.LOOPBACK

            name = mime_type.split('/')[1]
            self.modelName = f'DLNATest_{name}'
            self.friendlyName = self.modelName
            self.udn = get_udn(name.encode())

        def close(self):
            self.control_point.renderers.remove(self.renderer)

    def __init__(self, control_point, mime_type):
        root_device = self.RootDevice(self, mime_type, control_point)
        super().__init__(control_point, root_device.peer_ipaddress,
                         root_device)
        self.mime_type = mime_type

    async def play(self, speed=1):
        pass

    async def soap_action(self, serviceId, action, args='unused'):
        if action == 'GetProtocolInfo':
            return {'Source': None,
                    'Sink': f'http-get:*:{self.mime_type}:*'
                    }
        elif action == 'GetTransportInfo':
            state = ('PLAYING' if self.stream_sessions.is_playing else
                     'STOPPED')
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
        self.start_event = None
        self.upnp_control_point = None
        self.http_servers = {}          # {IPv4 address: http server instance}
        self.register_sem = asyncio.Semaphore()
        self.cp_tasks = AsyncioTasks()

    @log_exception(logger)
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

    @log_exception(logger)
    async def close(self, msg=None):
        # This coroutine may be run as a task by AVControlPoint.abort().
        try:
            if not self.closing:
                self.closing = True

                # The semaphore prevents a race condition where a new Renderer
                # is awaiting the registration of a sink with pulseaudio while
                # the list of renderers is being emptied here. In that case,
                # this sink would never be unregistered.
                async with self.register_sem:
                    for renderer in list(self.renderers):
                        await renderer.close()

                if self.pulse is not None:
                    await self.pulse.close()

                if sys.version_info[:2] >= (3, 9):
                    self.curtask.cancel(msg)
                else:
                    self.curtask.cancel()

        except Exception as e:
            logger.exception(f'Got exception {e!r}')

    def abort(self, msg):
        """Abort the whole program from a non-main task."""

        self.cp_tasks.create_task(self.close(msg), name='abort')
        raise ControlPointAbortError(msg)

    def disable_root_device(self, root_device, name=None):
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
                self.renderers.add(renderer)

        if registered:
            http_server = self.create_httpserver(renderer.local_ipaddress)
            http_server.allow_from(renderer.root_device.peer_ipaddress)
            self.cp_tasks.create_task(renderer.run(),
                                      name=renderer.nullsink.sink.name)

    def create_httpserver(self, ip_address):
        """Create the http_server task."""

        if ip_address not in self.http_servers:
            http_server = HTTPServer(self, ip_address, self.port)
            self.cp_tasks.create_task(http_server.run(),
                                      name=f'http_server-{ip_address}')
            self.http_servers[ip_address] = http_server
        return self.http_servers[ip_address]

    async def handle_upnp_notifications(self):
        while True:
            notif, root_device = await (
                                self.upnp_control_point.get_notification())
            logger.info(f"Got '{notif}' notification for {root_device}")

            # Ignore non Renderer devices.
            if re.match(rf'{MEDIARENDERER}(\d+)',
                        root_device.deviceType) is None:
                logger.info(f"Ignore '{root_device.modelName}': "
                            f'not a MediaRenderer')
                continue

            # Find an existing Renderer instance.
            for rndr in self.renderers:
                if rndr.root_device is root_device:
                    renderer = rndr
                    break
            else:
                renderer = None

            if notif == 'alive':
                if self.upnp_control_point.is_disabled(root_device):
                    logger.debug(f'Ignore disabled {root_device}')
                    continue

                if renderer is None:
                    local_ipaddress = root_device.local_ipaddress

                    # Find the local_ipaddress when processing a notify SSDP.
                    # Check that the root device peer_ipaddress belongs to
                    # one of the networks of our local network interfaces.
                    if local_ipaddress is None:
                        ip_addr = IPv4Address(root_device.peer_ipaddress)
                        for obj in ipv4_addresses(self.nics, yield_str=False):
                            if (isinstance(obj, IPv4Interface) and
                                    ip_addr in obj.network):
                                local_ipaddress = str(obj.ip)
                                break
                        else:
                            logger.warning(
                                f'Ignored: {root_device.peer_ipaddress} does'
                                f' not belong to one of the known network'
                                f' interfaces')
                            continue

                    renderer = Renderer(self, local_ipaddress, root_device)
                    await self.register(renderer)
            else:
                if renderer is not None:
                    if not renderer.closing:
                        await renderer.close()
                    else:
                        self.renderers.remove(renderer)
                else:
                    logger.warning("Got a 'byebye' notification for no"
                                   ' existing Renderer')

    @log_exception(logger)
    async def run_control_point(self):
        try:
            self.curtask = asyncio.current_task()
            self.start_event = asyncio.Event()

            if not self.config.any_available():
                sys.exit('Error: No encoder is available')

            self.parec_pgm = shutil.which('parec')
            if self.parec_pgm is None:
                sys.exit("Error: The pulseaudio 'parec' program cannot be found")

            # Add the signal handlers.
            end_event = asyncio.Event()
            self.cp_tasks.create_task(self.shutdown(end_event),
                                      name='shutdown')
            loop = asyncio.get_running_loop()
            for sig in (SIGINT, SIGTERM):
                loop.add_signal_handler(sig, end_event.set)

            # Run the UPnP control point.
            async with UPnPControlPoint(self.nics, self.msearch_interval,
                                        self.ttl) as self.upnp_control_point:
                # Create the Pulse task.
                self.pulse = Pulse(self)
                self.cp_tasks.create_task(self.pulse.run(), name='pulse')

                # Wait for the connection to PulseAudio to be ready.
                await self.start_event.wait()

                # Register the DLNATestDevices.
                for mtype in self.test_devices:
                    rndr = DLNATestDevice(self, mtype)
                    await self.register(rndr)

                # Handle UPnP notifications for ever.
                await self.handle_upnp_notifications()

        except asyncio.CancelledError as e:
            if e.args:
                logger.info(f'Main task got: {e!r}')
                return e.args[0]
        except Exception as e:
            logger.exception(f'Got exception {e!r}')
            return e
        finally:
            await self.close()

    def __str__(self):
        return 'pa-dlna'

# The main function.
def main():
    padlna_main(AVControlPoint, __doc__)

if __name__ == '__main__':
    padlna_main(AVControlPoint, __doc__)
