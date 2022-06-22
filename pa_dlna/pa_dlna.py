"""An Upnp control point that forwards PulseAudio streams to DLNA devices."""

import asyncio
import logging
import re
from signal import SIGINT, SIGTERM
from collections import namedtuple

from . import main_function, UPnPApplication
from .pulseaudio import Pulse
from .upnp import (UPnPControlPoint, UPnPClosedDeviceError, AsyncioTasks,
                   UPnPSoapFaultError)

logger = logging.getLogger('pa-dlna')

# Test with 'use_fake_renderer' as True when no DLNA device is available.
use_fake_renderer = 1 # XXX

MEDIARENDERER = 'urn:schemas-upnp-org:device:MediaRenderer:'
AVTRANSPORT = 'urn:upnp-org:serviceId:AVTransport'
RENDERINGCONTROL = 'urn:upnp-org:serviceId:RenderingControl'
CONNECTIONMANAGER = 'urn:upnp-org:serviceId:ConnectionManager'

SinkInputMetaData = namedtuple('SinkInputMetaData', ['application',
                                                     'artist',
                                                     'title'])

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
class MediaRenderer:
    """A DLNA MediaRenderer.

    See the Standardized DCP (SDCP) specifications:
      AVTransport:3 Service
      RenderingControl:3 Service
      ConnectionManager:3 Service
    """

    PULSE_RM_EVENTS = ('remove', 'exit')

    def __init__(self, root_device, av_control_point):
        self.root_device = root_device
        self.av_control_point = av_control_point
        self.closed = False
        self.nullsink = None            # A NullSink instance

    async def close(self):
        if not self.closed:
            self.closed = True
            if self.nullsink is not None:
                control_point = self.av_control_point
                await control_point.pulse.unregister(self)
                del control_point.renderers[self.nullsink.sink.index]
            self.root_device.close()

    async def register(self):
        control_point = self.av_control_point
        nullsink = await control_point.pulse.register(self)
        if nullsink is not None:
            self.nullsink = nullsink
            control_point.renderers[nullsink.sink.index] = self
            return True

    def log_event(self, event, sink, sink_input):
        if event in self.PULSE_RM_EVENTS:
            sink = self.nullsink.sink
            sink_input = self.nullsink.sink_input

        logger.debug(f"Sink-input {sink_input.index} '{event}' event for"
                    f" sink '{sink.name}' state '{sink.state._value}'")

    async def on_pulse_event(self, event, sink=None, sink_input=None):
        """Handle a PulseAudio event.

        'self.nullsink' holds the state prior to this event. The 'sink' and
        'sink_input' arguments define the new state.
        """

        if event in self.PULSE_RM_EVENTS:
            assert self.nullsink.sink_input is not None
        else:
            assert sink is not None and sink_input is not None
        self.log_event(event, sink, sink_input)

        # Process the event and set the new attributes values of nullsink.
        if event in self.PULSE_RM_EVENTS:
            logger.info(f"Stop the streaming to '{self.nullsink.sink.name}'")
            self.nullsink.sink_input = None

        else:
            curstate = sink.state._value
            prevstate = self.nullsink.sink.state._value
            if curstate == 'running':
                if prevstate != 'running':
                    logger.info(f"Start the streaming to '{sink.name}'")
                elif self.nullsink.sink_input is None:
                    logger.info(f"Back to the streaming to '{sink.name}'")
            elif curstate == 'idle' and prevstate == 'running':
                logger.info(f"Pause the streaming to '{sink.name}'")

            if event == 'change':
                prev_metadata = sink_input_meta(self.nullsink.sink_input)
                cur_metadata = sink_input_meta(sink_input)
                if cur_metadata is not None and cur_metadata != prev_metadata:
                    logger.debug(f'Playing {cur_metadata}')

            self.nullsink.sink = sink
            self.nullsink.sink_input = sink_input

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
        """Set up the MediaRenderer."""

        resp = await self.soap_action(CONNECTIONMANAGER, 'GetProtocolInfo',
                                      {})
class FakeMediaRenderer(MediaRenderer):
    """MediaRenderer to be used for testing when no DLNA device available."""

    class RootDevice:
        modelName = 'FakeMediaRenderer'
        friendlyName = 'This is a FakeMediaRenderer'

    def __init__(self, av_control_point):
        super().__init__(self.RootDevice(), av_control_point)

    async def close(self):
        if not self.closed:
            self.closed = True
            if self.nullsink is not None:
                control_point = self.av_control_point
                await control_point.pulse.unregister(self)
                del control_point.renderers[self.nullsink.sink.index]

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

    async def run_control_point(self):
        try:
            self.curtask = asyncio.current_task()

            end_event = asyncio.Event()
            asyncio.create_task(self.shutdown(end_event))
            loop = asyncio.get_running_loop()
            for sig in (SIGINT, SIGTERM):
                loop.add_signal_handler(sig, end_event.set)

            # Run the UPnP control point.
            async with UPnPControlPoint(self.ip_list,
                                        self.ttl) as control_point:
                # Create the Pulse task.
                self.pulse = Pulse(self)
                self.aio_tasks.create_task(self.pulse.run(), name='pulse')

                # Wait for the connection to PulseAudio to be ready.
                await self.start_event.wait()

                if use_fake_renderer:
                    renderer = FakeMediaRenderer(self)
                    await renderer.register()

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
                            renderer = MediaRenderer(root_device, self)
                            if await renderer.register():
                                await renderer.run()
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
    main_function(AVControlPoint, __doc__, logger)
