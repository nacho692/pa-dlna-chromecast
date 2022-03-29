"""The interface to pulseaudio.
"""

import asyncio
import logging
from pa_dlna.upnp import UPnPControlPoint, AsyncioTasks

logger = logging.getLogger('pulse')

class Pulseaudio:

    def __init__(self, ipaddr_list, ttl):
        self.ipaddr_list = ipaddr_list
        self.ttl = ttl
        self.closed = False
        self.devices = {}               # {pulseaudio index: UpnpDevice}
        self.aio_tasks = AsyncioTasks()

    async def run(self):
        try:
            with UPnPControlPoint(self.ipaddr_list, self.ttl) as upnp:
                notif_type, upnp_device = await upnp.get_notification()
                logger.debug(f'got {(notif_type, upnp_device)}')
                await asyncio.sleep(3600) # XXX
        except Exception as e:
            logger.exception(e)
        finally:
            self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.aio_tasks.cancel_all()
            logger.debug('end of pulseaudio task')
