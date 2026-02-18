"""Chromecast discovery and device helpers."""

import asyncio
import logging
import socket

from .upnp import QUEUE_CLOSED

logger = logging.getLogger('chromecast')

CHROMECAST_MIME_TYPES = (
    'audio/mpeg',
    'audio/mp3',
    'audio/flac',
    'audio/x-flac',
    'audio/wav',
    'audio/aac',
    'audio/x-aac',
    'audio/ogg',
    'audio/opus',
    'audio/vorbis',
)


class CastRootDevice:
    """Adapter used by AVControlPoint to handle Chromecast devices."""

    source = 'chromecast'

    def __init__(self, cast_info, local_ipaddress):
        self.closed = False
        self.cast = None
        self.cast_info = cast_info
        self.local_ipaddress = local_ipaddress
        self._update(cast_info)

    @staticmethod
    def _get_attr(cast_info, attr, default=None):
        value = getattr(cast_info, attr, default)
        return default if value is None else value

    def _update(self, cast_info):
        self.cast_info = cast_info
        cast_uuid = str(self._get_attr(cast_info, 'uuid', 'unknown'))
        if not cast_uuid.startswith('uuid:'):
            cast_uuid = f'uuid:{cast_uuid}'

        self.UDN = cast_uuid
        self.udn = cast_uuid
        self.modelName = self._get_attr(cast_info, 'model_name', 'Chromecast')
        self.friendlyName = self._get_attr(cast_info, 'friendly_name',
                                           self.modelName)
        self.peer_ipaddress = self._get_attr(cast_info, 'host', '127.0.0.1')
        self.port = self._get_attr(cast_info, 'port', 8009)

    def refresh(self, cast_info, local_ipaddress):
        self.local_ipaddress = local_ipaddress
        self._update(cast_info)

    def close(self):
        if self.closed:
            return

        self.closed = True
        if self.cast is not None:
            try:
                self.cast.disconnect(timeout=1)
            except Exception:
                pass
            finally:
                self.cast = None

    def __str__(self):
        return (f"ChromecastRootDevice('{self.friendlyName}',"
                f" '{self.udn}')")


class ChromecastProvider:
    """Discover Chromecast devices and expose alive/byebye notifications."""

    def __init__(self, control_point):
        self.control_point = control_point
        self.pychromecast = None
        self.zeroconf = None
        self.browser = None
        self.listener = None
        self.started = False
        self.closed = False
        self.loop = None
        self.faulty_devices = set()
        self.devices = {}    # {str(uuid): CastRootDevice}
        self.notifications = asyncio.Queue()

    @staticmethod
    def _import_pychromecast():
        try:
            import pychromecast
            import zeroconf
            from pychromecast.discovery import CastBrowser, SimpleCastListener
            return pychromecast, zeroconf, CastBrowser, SimpleCastListener
        except ImportError:
            return None, None, None, None

    def _enqueue_notification(self, kind, cast_uuid):
        if self.closed or self.loop is None:
            return
        cast_uuid = str(cast_uuid)
        self.loop.call_soon_threadsafe(self.notifications.put_nowait,
                                       (kind, cast_uuid))

    def _cast_info(self, cast_uuid):
        if self.browser is None:
            return None

        try:
            devices = list(self.browser.devices.items())
        except Exception:
            return None

        for key, info in devices:
            info_uuid = getattr(info, 'uuid', key)
            if str(info_uuid) == cast_uuid:
                return info
        return None

    def _resolve_local_ip(self, peer_ipaddress):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((peer_ipaddress, 9))
                return sock.getsockname()[0]
        except OSError:
            if getattr(self.control_point, 'ip_addresses', None):
                if self.control_point.ip_addresses:
                    return self.control_point.ip_addresses[0]
            return '127.0.0.1'

    async def start(self):
        self.loop = asyncio.get_running_loop()
        (self.pychromecast, zeroconf_module,
            cast_browser, cast_listener) = self._import_pychromecast()
        if self.pychromecast is None:
            logger.warning("Chromecast support is disabled:"
                           " missing 'pychromecast' dependency")
            return False

        try:
            self.zeroconf = zeroconf_module.Zeroconf()
            self.listener = cast_listener(
                add_callback=lambda cast_uuid, service:
                    self._enqueue_notification('alive', cast_uuid),
                remove_callback=lambda cast_uuid, service, cast_info:
                    self._enqueue_notification('byebye', cast_uuid),
                update_callback=lambda cast_uuid, service:
                    self._enqueue_notification('alive', cast_uuid),
            )
            self.browser = cast_browser(self.listener, self.zeroconf)
            self.browser.start_discovery()
            self.started = True
            logger.info('Start Chromecast discovery')
            return True
        except Exception as e:
            logger.warning(f'Cannot start Chromecast discovery: {e!r}')
            await self.close()
            return False

    async def close(self):
        if self.closed:
            return

        self.closed = True
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.notifications.put_nowait,
                                           QUEUE_CLOSED)

        try:
            if self.browser is not None:
                self.browser.stop_discovery()
        except Exception:
            pass

        try:
            if self.zeroconf is not None:
                self.zeroconf.close()
        except Exception:
            pass

        for root_device in list(self.devices.values()):
            root_device.close()
        self.devices.clear()

        logger.info('Close Chromecast provider')

    def disable_root_device(self, root_device, name=None):
        udn = root_device.udn
        if udn in self.faulty_devices:
            return

        self.faulty_devices.add(udn)
        root_device.close()
        if name is None:
            name = f'{root_device.friendlyName} Chromecast'
        logger.warning(f'Disable the {name} device permanently')

    def is_disabled(self, root_device):
        return root_device.udn in self.faulty_devices

    async def get_notification(self):
        while True:
            notif, cast_uuid = await self.notifications.get()
            if (notif, cast_uuid) == QUEUE_CLOSED:
                return QUEUE_CLOSED

            if notif == 'alive':
                cast_info = self._cast_info(cast_uuid)
                if cast_info is None:
                    continue

                local_ipaddress = self._resolve_local_ip(cast_info.host)
                root_device = self.devices.get(cast_uuid)
                if root_device is None:
                    root_device = CastRootDevice(cast_info, local_ipaddress)
                    self.devices[cast_uuid] = root_device
                else:
                    root_device.refresh(cast_info, local_ipaddress)
                return notif, root_device

            if notif == 'byebye':
                root_device = self.devices.pop(cast_uuid, None)
                if root_device is None:
                    continue
                return notif, root_device

    async def get_chromecast(self, root_device):
        if root_device.cast is not None:
            return root_device.cast

        loop = asyncio.get_running_loop()

        def connect():
            cast = self.pychromecast.get_chromecast_from_cast_info(
                root_device.cast_info,
                zconf=self.zeroconf,
                timeout=5)
            cast.wait()
            return cast

        root_device.cast = await loop.run_in_executor(None, connect)
        return root_device.cast
