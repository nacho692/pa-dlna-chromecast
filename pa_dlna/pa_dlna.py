"""An Upnp control point that forwards PulseAudio streams to DLNA devices."""

import asyncio
import logging
import re
from signal import SIGINT, SIGTERM

from . import main_function, UPnPApplication
from .pulseaudio import PulseEvent, Pulse, NullSink
from .upnp import (UPnPControlPoint, UPnPClosedDeviceError, AsyncioTasks,
                   UPnPSoapFaultError)

logger = logging.getLogger('pa-dlna')

# Test with 'use_fake_renderer' as True when no DLNA device is available.
use_fake_renderer = 0

MEDIARENDERER = 'urn:schemas-upnp-org:device:MediaRenderer:'
AVTRANSPORT = 'urn:upnp-org:serviceId:AVTransport'
RENDERINGCONTROL = 'urn:upnp-org:serviceId:RenderingControl'
CONNECTIONMANAGER = 'urn:upnp-org:serviceId:ConnectionManager'

# Classes.
class MediaRenderer:
    """A DLNA MediaRenderer.

    See the Standardized DCP (SDCP) specifications:
      AVTransport:3 Service
      RenderingControl:3 Service
      ConnectionManager:3 Service
    """

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
                del control_point.renderers[self.nullsink.index]
            self.root_device.close()

    async def on_pulse_event(self, event):
        """Handle a PulseAudio event."""

        assert isinstance(event, PulseEvent)

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
        modelName = 'R-N402D'
        friendlyName = 'Yamaha RN402D'

    def __init__(self, av_control_point):
        super().__init__(self.RootDevice(), av_control_point)

    async def close(self):
        if not self.closed:
            self.closed = True
            if self.nullsink is not None:
                control_point = self.av_control_point
                await control_point.pulse.unregister(self)
                del control_point.renderers[self.nullsink.index]

    async def on_pulse_event(self, event):
        assert isinstance(event, PulseEvent)

class AVControlPoint(UPnPApplication):
    """Control point with Content.

    Manage PulseAudio and the DLNA MediaRenderer devices.
    See section 6.6 of "UPnP AV Architecture:2".
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closed = False
        self.renderers = {}             # dict {nullsink.index: MediaRenderer}
        self.curtask = None             # task running run_control_point()
        self.pulse = None               # Pulse instance
        self.start_event = asyncio.Event()
        self.end_event = asyncio.Event()
        self.aio_tasks = AsyncioTasks()

    async def shutdown(self):
        try:
            await self.end_event.wait()
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
                self.pulse.close()

            self.aio_tasks.cancel_all()

    async def register(self, renderer):
        nullsink = await self.pulse.register(renderer)
        if nullsink is not None:
            renderer.nullsink = nullsink
            self.renderers[nullsink.index] = renderer
            return True

    async def run_control_point(self):
        try:
            self.curtask = asyncio.current_task()

            loop = asyncio.get_running_loop()
            asyncio.create_task(self.shutdown())
            for sig in (SIGINT, SIGTERM):
                loop.add_signal_handler(sig, self.end_event.set)

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
                    await self.register(renderer)

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
                            if await self.register(renderer):
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
