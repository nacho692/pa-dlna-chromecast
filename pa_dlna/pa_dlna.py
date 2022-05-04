"""An Upnp control point that forwards Pulseaudio streams to DLNA devices."""

import logging
import re
import asyncio

from . import (main_function, UPnPApplication)
from .upnp import (UPnPControlPoint, UPnPClosedDeviceError, AsyncioTasks,
                   UPnPSoapFaultError)

logger = logging.getLogger('pa-dlna')

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

    def __init__(self, root_device, padlna):
        self.root_device = root_device
        self.padlna = padlna

    def close(self):
        self.root_device.close()

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
            self.close()

    async def run(self):
        """Set up the MediaRenderer."""

        resp = await self.soap_action(CONNECTIONMANAGER, 'GetProtocolInfo',
                                      {})

class AVControlPoint(UPnPApplication):
    """Control point with Content.

    Manage Pulseaudio and the DLNA MediaRenderer devices.
    See section 6.6 of "UPnP AV Architecture:2".
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closed = False
        self.devices = {}               # dict {UPnPDevice: MediaRenderer}
        self.aio_tasks = AsyncioTasks()

    def close(self):
        if not self.closed:
            self.closed = True
            for renderer in self.devices.values():
                renderer.close()
            self.devices = {}
            self.aio_tasks.cancel_all()

    def remove_device(self, device):
        if device in self.devices:
            del self.devices[device]

    async def run_control_point(self):
        try:
            # Run the UPnP control point.
            async with UPnPControlPoint(self.ipaddr_list,
                                        self.ttl) as control_point:
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
            self.close()

    def __str__(self):
        return 'pa-dlna'

# The main function.
if __name__ == '__main__':
    main_function(AVControlPoint, __doc__, logger)
