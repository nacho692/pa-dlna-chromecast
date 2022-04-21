"""Redirect pulseaudio streams to DLNA MediaRenderers."""

import logging
from pa_dlna.upnp.upnp import UPnPControlPoint

logger = logging.getLogger('pulse')

class Pulseaudio:
    """XXX."""

    def __init__(self, ipaddr_list, ttl):
        self.ipaddr_list = ipaddr_list
        self.ttl = ttl
        self.closed = False
        self.devices = {}               # {pulseaudio index: UpnpDevice}

    async def run(self):
        try:
            with UPnPControlPoint(self.ipaddr_list, self.ttl) as upnp:
                while True:
                    notification, root_device = await upnp.get_notification()
                    logger.info(f'Got notification'
                                f' {(notification, root_device)}')
        except (KeyboardInterrupt, SystemExit):
            pass
        except Exception as e:
            logger.exception(f'{e!r}')
        finally:
            self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            logger.debug('End of pulseaudio task')
