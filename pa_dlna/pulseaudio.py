"""The interface to pulseaudio.
"""

import asyncio
import logging
from pa_dlna.upnp import UPnPControlPoint, AsyncioTasks

logger = logging.getLogger('pulse')

class Pulseaudio:

    def __init__(self, ipaddr_list, ttl, aging):
        self.ipaddr_list = ipaddr_list
        self.ttl = ttl
        self.aging = aging
        self.closed = False
        self.devices = {}               # {pulseaudio index: UpnpDevice}
        self.aio_tasks = AsyncioTasks()

    async def run(self):
        try:
            with (UPnPControlPoint(self.ipaddr_list, self.ttl, self.aging)
                  as upnp):
                notification, root_device = await upnp.get_notification()
                logger.info(f'Got notification {(notification, root_device)}')

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
