"""The interface to pulseaudio.
"""

import asyncio
import logging

logger = logging.getLogger('pulse')

class Pulseaudio:

    def __init__(self, loop, upnp):
        self.loop = loop
        self.upnp = upnp
        self.devices = {}
        upnp.register_cb(self.weakref_callback)

    def weakref_callback(self, ref):
        """Callback called by the Upnp instance when a device is finalized."""
        for udn, device in list(self.devices.items()):
            if device == ref:
                del self.devices[udn]

    async def run(self):
        try:
            await asyncio.sleep(100)
        except Exception as e:
            logger.exception(e)
        finally:
            logger.info('end of task pulseaudio')
