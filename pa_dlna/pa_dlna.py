"""An Upnp control point that forwards PulseAudio streams to DLNA devices."""

import asyncio
import logging
import re

from . import (main_function, UPnPApplication)
from .pulseaudio import (PulseEvent, PulseAudio)
from .upnp import (UPnPControlPoint, UPnPClosedDeviceError, AsyncioTasks,
                   UPnPSoapFaultError)

logger = logging.getLogger('pa-dlna')

# Set 'use_fake_renderer' to True when no DLNA device is available.
use_fake_renderer = 1 # XXX

MEDIARENDERER = 'urn:schemas-upnp-org:device:MediaRenderer:'
AVTRANSPORT = 'urn:upnp-org:serviceId:AVTransport'
RENDERINGCONTROL = 'urn:upnp-org:serviceId:RenderingControl'
CONNECTIONMANAGER = 'urn:upnp-org:serviceId:ConnectionManager'

# Class(es).
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
        self.sink_index = None          # null-sink index
        self.module_index = None        # and the corresponding module index

    async def close(self):
        pulse = self.av_control_point.pulse
        await pulse.unregister(self)
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
        pulse = self.av_control_point.pulse
        await pulse.register(self)

class FakeMediaRenderer(MediaRenderer):
    """MediaRenderer to be used for testing when no DLNA device available."""

    class RootDevice:
        modelName = 'R-N402D'
        friendlyName = 'Yamaha RN402D'

    def __init__(self, av_control_point):
        super().__init__(self.RootDevice(), av_control_point)

    async def close(self):
        pulse = self.av_control_point.pulse
        await pulse.unregister(self)

    async def on_pulse_event(self, event):
        assert isinstance(event, PulseEvent)

    async def run(self):
        pulse = self.av_control_point.pulse
        await pulse.register(self)

class AVControlPoint(UPnPApplication):
    """Control point with Content.

    Manage PulseAudio and the DLNA MediaRenderer devices.
    See section 6.6 of "UPnP AV Architecture:2".
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closed = False
        self.devices = {}               # dict {UPnPDevice: MediaRenderer}
        self.curtask = None             # task running run_control_point()
        self.pulse = None               # PulseAudio instance
        self.event = asyncio.Event()
        self.aio_tasks = AsyncioTasks()

    async def close(self):
        if not self.closed:
            self.closed = True
            for renderer in self.devices.values():
                await renderer.close()

            if self.pulse is not None:
                self.pulse.close()

            self.aio_tasks.cancel_all()

    def remove_device(self, device):
        if device in self.devices:
            del self.devices[device]

    async def run_control_point(self):
        try:
            self.curtask = asyncio.current_task()

            # Run the UPnP control point.
            async with UPnPControlPoint(self.ip_list,
                                        self.ttl) as control_point:
                # Create the PulseAudio task.
                self.pulse = PulseAudio(self)
                self.aio_tasks.create_task(self.pulse.run(), name='pulseaudio')

                # Wait for the connection to PulseAudio to be ready.
                await self.event.wait()
                if use_fake_renderer:
                    renderer = FakeMediaRenderer(self)
                    await renderer.run()

                # Handle UPnP notifications.
                while True:
                    notif, root_device = await control_point.get_notification()
                    logger.info(f'Got notification'
                                f' {(notif, root_device)}')

                    # Ignore non MediaRenderer devices.
                    if re.match(rf'{MEDIARENDERER}(1|2)',
                                root_device.deviceType) is None:
                        continue

                    if notif == 'alive':
                        if root_device not in self.devices:
                            renderer = MediaRenderer(root_device, self)
                            await renderer.run()
                            self.devices[root_device] = renderer
                    else:
                        self.remove_device(root_device)

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
    main_function(AVControlPoint, __doc__, logger)
