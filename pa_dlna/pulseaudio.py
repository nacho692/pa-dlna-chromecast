"""Redirect pulseaudio streams to DLNA MediaRenderers."""

import logging
import asyncio
import re
from pa_dlna.upnp.upnp import (UPnPControlPoint, UPnPClosedDeviceError,
                               AsyncioTasks)

# Pulseaudio exceptions.
class PulseError(Exception): pass
class PulseSoapError(PulseError): pass

logger = logging.getLogger('pulse')

MEDIARENDERER = 'urn:schemas-upnp-org:device:MediaRenderer:'
AVTRANSPORT = 'urn:upnp-org:serviceId:AVTransport'
RENDERINGCONTROL = 'urn:upnp-org:serviceId:RenderingControl'
CONNECTIONMANAGER = 'urn:upnp-org:serviceId:ConnectionManager'

class MediaRenderer:
    """A DLNA MediaRenderer.

    See the Standardized DCP (SDCP):
      UPnP AV Architecture:2
      AVTransport:3 Service
      RenderingControl:3 Service
      ConnectionManager:3 Service
    """

    def __init__(self, upnp_device, pulse):
        self.upnp_device = upnp_device

    async def soap_action(self, serviceId, action, args):
        """XXX."""

        service = self.upnp_device.serviceList.get(serviceId)
        try:
            res =  await service.soap_action(action, args)
            logger.debug(f"soap_action('{action}') = {res}")
            return res
        except UPnPClosedDeviceError:
            logger.warning(f'soap_action() failed: {service.root_device}'
                           f' is closed')

    async def run(self):
        """Set up the MediaRenderer."""

        # XXX
        res = await self.soap_action(AVTRANSPORT, 'GetMediaInfo',
                                     {'InstanceID': 0})

class Pulseaudio:
    """XXX."""

    def __init__(self, ipaddr_list, ttl):
        self.ipaddr_list = ipaddr_list
        self.ttl = ttl
        self.closed = False
        self.devices = {}               # dict {UpnpDevice: MediaRenderer}
        self.aio_tasks = AsyncioTasks()

    def remove_device(self, device):
        del self.devices[root_device]

    async def run(self):
        try:
            async with UPnPControlPoint(self.ipaddr_list, self.ttl) as upnp:
                while True:
                    notification, root_device = await upnp.get_notification()
                    logger.info(f'Got notification'
                                f' {(notification, root_device)}')

                    if re.match(rf'{MEDIARENDERER}(1|2)',
                                root_device.deviceType) is None:
                        continue

                    if notification == 'alive':
                        if root_device not in self.devices:
                            renderer = MediaRenderer(root_device, self)
                            await renderer.run()
                            self.devices[root_device] = renderer
                    else:
                        del self.devices[root_device]

                    # XXX
                    await asyncio.sleep(10)
                    root_device.close()
        except Exception as e:
            logger.exception(f'Got exception {e!r}')
        except asyncio.CancelledError as e:
            logger.error(f'Got exception {e!r}')
        finally:
            self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.aio_tasks.cancel_all()
            logger.debug('End of pulseaudio')
