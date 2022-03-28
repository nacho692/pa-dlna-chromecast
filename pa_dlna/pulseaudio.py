"""The interface to pulseaudio.
"""

import asyncio
import logging

logger = logging.getLogger('pulse')

class Pulseaudio:

    def __init__(self, upnp):
        self.upnp = upnp
        self.devices = {}               # {pulseaudio index: UpnpDevice}

    async def run(self):
        try:
            await asyncio.sleep(3600)
        except Exception as e:
            logger.exception(e)
        finally:
            logger.debug('end of pulseaudio task')
