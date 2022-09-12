"""An Upnp control point that forwards PulseAudio streams to DLNA devices."""

import sys
import shutil
import asyncio
import logging
import re
import ipaddress
from signal import SIGINT, SIGTERM
from collections import namedtuple

from . import main_function, UPnPApplication
from .pulseaudio import Pulse
from .http_server import HTTPServer
from .encoders import select_encoder
from .upnp import (UPnPControlPoint, UPnPClosedDeviceError, AsyncioTasks,
                   UPnPSoapFaultError)

logger = logging.getLogger('pa-dlna')

# Set 'use_fake_renderer' to True for testing when no available DLNA device.
use_fake_renderer = 1 # XXX

MEDIARENDERER = 'urn:schemas-upnp-org:device:MediaRenderer:'
AVTRANSPORT = 'urn:upnp-org:serviceId:AVTransport'
RENDERINGCONTROL = 'urn:upnp-org:serviceId:RenderingControl'
CONNECTIONMANAGER = 'urn:upnp-org:serviceId:ConnectionManager'

SinkInputMetaData = namedtuple('SinkInputMetaData', ['application',
                                                     'artist',
                                                     'title'])
async def close_stream(stream):
    try:
        await stream.drain()
        stream.close()
        await writer.wait_closed()
    except Exception:
        pass

def sink_input_meta(sink_input):
    if sink_input is None:
        return None

    proplist = sink_input.proplist
    try:
        return SinkInputMetaData(proplist['application.name'],
                                 proplist['media.artist'],
                                 proplist['media.title'])
    except KeyError:
        pass

# Classes.
class Stream:
    """An audio stream.

    The stream is made of two processes and an asyncio Stream Writer connected
    through pipes:
        - 'parec' records the audio from the nullsink monitor and forwards it
          to the encoder program.
        - The encoder program encodes the audio according to the encoder
          protocol and forwards it to the Stream Writer.
        - The Stream Writer writes the stream to the HTTP socket.
    """

    def __init__(self, renderer):
        self.renderer = renderer
        self.writer = None

    async def stop(self):
        if self.writer is not None:
            try:
                logger.debug(f"Stop '{self.renderer.name}' stream")
                await close_stream(self.writer)
            finally:
                self.writer = None

    async def start(self, writer):
        monitor = self.renderer.nullsink.sink.monitor_source_name
        parec_pgm = self.renderer.control_point.parec_pgm
        parec_cmd = [parec_pgm, f'--device={monitor}', '--format=s16le']
        encoder_cmd = self.renderer.encoder.command

        if self.writer is not None:
            logger.debug(f"Cannot start '{self.renderer.name}' stream "
                         f'(a stream is already running)')
            await close_stream(writer)
            return

        self.writer = writer
        logger.debug(f"Start '{self.renderer.name}' stream")
        try:
            # start the processes - write 200 OK and headers - pipe to socket
            writer.write(f'HTTP/1.1 200 OK\r\n'
                         f'Content-type: {self.renderer.mime_type}\r\n'
                         f'\r\n'
                         f'Test with FakeMediaRenderer.'.encode())

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f'{e!r}')
        finally:
            await self.stop()

class MediaRenderer:
    """A DLNA MediaRenderer.

    Attributes:
      net_iface     The control point ipaddress.IPv4Interface network
                    interface that the DLNA device belongs to

    See the Standardized DCP (SDCP) specifications:
      AVTransport:3 Service
      RenderingControl:3 Service
      ConnectionManager:3 Service
    """

    PULSE_RM_EVENTS = ('remove', 'exit')

    def __init__(self, control_point, net_iface, root_device):
        self.control_point = control_point
        self.net_iface = net_iface
        self.root_device = root_device
        self.closed = False
        self.nullsink = None            # NullSink instance
        self.name = None                # NullSink name
        self.encoder = None
        self.mime_type = None
        self.stream = Stream(self)
        self.pulse_queue = asyncio.Queue()

    async def close(self):
        if not self.closed:
            self.closed = True
            logger.info(f"Close '{self.name}' renderer")
            if self.nullsink is not None:
                await self.control_point.pulse.unregister(self)
                del self.control_point.renderers[self.nullsink.sink.index]
            self.root_device.close()

    async def register(self):
        nullsink = await self.control_point.pulse.register(self)
        if nullsink is not None:
            self.nullsink = nullsink
            self.name = nullsink.sink.name
            self.control_point.renderers[nullsink.sink.index] = self
            return True

    def start_stream(self, writer, uri_path):
        """Start the streaming task."""

        if uri_path.strip('/') == self.root_device.udn:
            task_name = f'stream-{self.name}'
            self.control_point.aio_tasks.create_task(
                                self.stream.start(writer), name=task_name)
            return True

    def log_event(self, event, sink, sink_input):
        if event in self.PULSE_RM_EVENTS:
            sink = self.nullsink.sink
            sink_input = self.nullsink.sink_input

        logger.debug(f"Sink-input {sink_input.index} '{event}' event for"
                    f" sink '{sink.name}' state '{sink.state._value}'")

    async def on_pulse_event(self, event, sink=None, sink_input=None):
        """Handle a PulseAudio event.

        This coroutine is run by the 'pulse' task.
        'self.nullsink' holds the state prior to this event. The 'sink' and
        'sink_input' arguments define the new state.
        """

        if event in self.PULSE_RM_EVENTS:
            assert self.nullsink.sink_input is not None
        else:
            assert sink is not None and sink_input is not None
        self.log_event(event, sink, sink_input)

        avtransport_events = []
        # Process the event and set the new attributes values of nullsink.
        if event in self.PULSE_RM_EVENTS:
            avtransport_events.append('stop')
            logger.info(f"Stop the streaming to '{self.nullsink.sink.name}'")
            self.nullsink.sink_input = None

        else:
            curstate = sink.state._value
            prevstate = self.nullsink.sink.state._value
            if curstate == 'running':
                if prevstate != 'running':
                    avtransport_events.append('start')
                    logger.info(f"Start the streaming to '{sink.name}'")
                elif self.nullsink.sink_input is None:
                    avtransport_events.append('start')
                    logger.info(f"Back to the streaming to '{sink.name}'")
            elif curstate == 'idle' and prevstate == 'running':
                avtransport_events.append('pause')
                logger.info(f"Pause the streaming to '{sink.name}'")

            if event == 'change':
                prev_metadata = sink_input_meta(self.nullsink.sink_input)
                cur_metadata = sink_input_meta(sink_input)
                if cur_metadata is not None and cur_metadata != prev_metadata:
                    avtransport_events.append(cur_metadata)
                    logger.debug(f'Playing {cur_metadata}')

            self.nullsink.sink = sink
            self.nullsink.sink_input = sink_input

        for evt in avtransport_events:
            self.pulse_queue.put_nowait(evt)

    async def soap_action(self, serviceId, action, args):
        """Send a SOAP action.

        Return the dict {argumentName: out arg value} if successfull,
        otherwise an instance of the upnp.xml.SoapFault namedtuple defined by
        field names in ('errorCode', 'errorDescription').
        """

        try:
            service = self.root_device.serviceList[serviceId]
            return await service.soap_action(action, args)
        except UPnPSoapFaultError as e:
            return e.args[0]
        except UPnPClosedDeviceError:
            logger.error(f'soap_action(): root device {self.root_device} is'
                         f' closed')
        except Exception as e:
            logger.exception(f'{e!r}')
            await self.close()

    async def run(self):
        """Run the MediaRenderer task."""

        try:
            # Select an encoder matching the DLNA device supported mime types.
            if not isinstance(self, FakeMediaRenderer): # XXX
                protocols = await self.soap_action(CONNECTIONMANAGER,
                                                   'GetProtocolInfo', {})
            else:
                protocols = ['audio/mp3']

            res = select_encoder(self.control_point.encoders, protocols,
                                 self.root_device.udn)
            if res is not None:
                self.encoder = res[0]
                self.mime_type = res[1]
                logger.info(f"Select '{self.encoder.__class__.__name__}'"
                            f" encoder for the '{self.name}' renderer")
            else:
                logger.error(f'Cannot find an encoder matching the '
                             f"{self.name} supported mime types")
                await self.close()
                return

            while True:
                # An AVTransport event is either 'start', 'stop', 'pause' or
                # an instance of SinkInputMetaData.
                avtransport_event = await self.pulse_queue.get()
                logger.debug(f'XXX {avtransport_event}')

        except asyncio.CancelledError:
            await self.close()
        except Exception as e:
            logger.exception(f'{e!r}')
            await self.close()

class FakeMediaRenderer(MediaRenderer):
    """MediaRenderer to be used for testing when no DLNA device available."""

    class RootDevice:
        udn = 'e70e9d0e-bbbb-dddd-eeee-ffffffffffff'
        ip_source = '127.0.0.1'
        modelName = 'FakeMediaRenderer'
        friendlyName = 'This is a FakeMediaRenderer'

        def close(self):
            pass

    def __init__(self, control_point):
        super().__init__(control_point,
                         ipaddress.IPv4Interface('127.0.0.1/8'),
                         self.RootDevice())

class AVControlPoint(UPnPApplication):
    """Control point with Content.

    Manage PulseAudio and the DLNA MediaRenderer devices.
    See section 6.6 of "UPnP AV Architecture:2".
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closed = False
        self.renderers = {}     # dict {nullsink.sink.index: MediaRenderer}
        self.curtask = None     # task running run_control_point()
        self.pulse = None       # Pulse instance
        self.start_event = asyncio.Event()
        self.aio_tasks = AsyncioTasks()

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
        if not self.closed:
            self.closed = True
            for renderer in list(self.renderers.values()):
                await renderer.close()

            if self.pulse is not None:
                await self.pulse.close()

            self.aio_tasks.cancel_all()
            self.curtask.cancel()

    async def register(self, renderer):
        """Load the null-sink module and create the renderer task."""

        if await renderer.register():
            self.aio_tasks.create_task(renderer.run(),
                                       name=renderer.nullsink.sink.name)

    async def run_control_point(self):
        if not any(enc.available for enc in self.encoders.values()):
            sys.exit('Error: No encoder is available')

        self.parec_pgm = shutil.which('parec')
        if self.parec_pgm is None:
            sys.exit("Error: The pulseaudio 'parec' program cannot be found")

        try:
            self.curtask = asyncio.current_task()

            end_event = asyncio.Event()
            asyncio.create_task(self.shutdown(end_event))
            loop = asyncio.get_running_loop()
            for sig in (SIGINT, SIGTERM):
                loop.add_signal_handler(sig, end_event.set)

            # Run the UPnP control point.
            async with UPnPControlPoint(self.net_ifaces,
                                        self.ttl) as control_point:
                # Create the Pulse task.
                self.pulse = Pulse(self)
                self.aio_tasks.create_task(self.pulse.run(), name='pulse')

                # Wait for the connection to PulseAudio to be ready.
                await self.start_event.wait()

                # Create the http_server task.
                http_server = HTTPServer(self.renderers, self.net_ifaces,
                                         self.port)
                self.aio_tasks.create_task(http_server.run(),
                                           name='http_server')

                if use_fake_renderer:
                    fake_rndr = FakeMediaRenderer(self)
                    http_server.allow_from(fake_rndr.root_device.ip_source)
                    await self.register(fake_rndr)

                # Handle UPnP notifications.
                while True:
                    notif, root_device = await control_point.get_notification()
                    logger.info(f'Got notification'
                                f' {(notif, root_device)}')

                    # Ignore non MediaRenderer devices.
                    if re.match(rf'{MEDIARENDERER}(1|2)',
                                root_device.deviceType) is None:
                        continue

                    # Find an existing MediaRenderer instance.
                    for rndr in self.renderers.values():
                        if rndr.root_device is root_device:
                            renderer = rndr
                            break
                    else:
                        renderer = None

                    if notif == 'alive':
                        if renderer is None:
                            # Check that ip_source belongs to one of the
                            # net_ifaces networks.
                            ip_source = root_device.ip_source
                            ip_obj = ipaddress.IPv4Address(ip_source)
                            for net_iface in self.net_ifaces:
                                if ip_obj in net_iface.network:
                                    break
                            else:
                                logger.warning(f'{ip_source} does not belong'
                                            ' to one of the enabled networks')
                                return
                            http_server.allow_from(ip_source)
                            await self.register(MediaRenderer(self, net_iface,
                                                              root_device))
                    else:
                        if renderer is not None:
                            renderer.close()
                        else:
                            logger.warning("Got a 'byebye' notification for"
                                           ' no existing MediaRenderer')

        except Exception as e:
            logger.exception(f'Got exception {e!r}')
            await self.close()

    def __str__(self):
        return 'pa-dlna'

# The main function.
if __name__ == '__main__':
    main_function(AVControlPoint, __doc__)
