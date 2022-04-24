"""Redirect pulseaudio streams to DLNA MediaRenderers."""

import logging
import asyncio
from pa_dlna.upnp.upnp import UPnPControlPoint, AsyncioTasks

logger = logging.getLogger('pulse')

class Pulseaudio:
    """XXX."""

    def __init__(self, ipaddr_list, ttl):
        self.ipaddr_list = ipaddr_list
        self.ttl = ttl
        self.closed = False
        self.devices = {}               # {pulseaudio index: UpnpDevice}
        self.aio_tasks = AsyncioTasks()

    async def run(self):
        try:
            async with UPnPControlPoint(self.ipaddr_list, self.ttl) as upnp:
                while True:
                    notification, root_device = await upnp.get_notification()
                    logger.info(f'Got notification'
                                f' {(notification, root_device)}')
                    await asyncio.sleep(10)
                    root_device.close()
        except Exception as e:
            logger.exception(f'{e!r}')
        except asyncio.CancelledError as e:
            logger.error(f'{e!r}')
        finally:
            self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.aio_tasks.cancel_all()
            logger.debug('End of pulseaudio task')
