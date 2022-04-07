"""Redirect pulseaudio streams to DLNA MediaRenderers."""

import logging
from pa_dlna.upnp import UPnPControlPoint

logger = logging.getLogger('pulse')

class Pulseaudio:
    """XXX."""

    def __init__(self, ipaddr_list, ttl, aging):
        self.ipaddr_list = ipaddr_list
        self.ttl = ttl
        self.aging = aging
        self.closed = False
        self.devices = {}               # {pulseaudio index: UpnpDevice}

    async def run(self):
        try:
            with (UPnPControlPoint(self.ipaddr_list, self.ttl, self.aging)
                  as upnp):
                while True:
                    notification, root_device = await upnp.get_notification()
                    logger.info(f'Got notification'
                                f' {(notification, root_device)}')
        except (KeyboardInterrupt, SystemExit):
            pass
        except Exception as e:
            logger.exception(f'{repr(e)}')
        finally:
            self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            logger.debug('end of pulseaudio task')
