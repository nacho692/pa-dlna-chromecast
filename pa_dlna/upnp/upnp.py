"""A basic UPnP Control Point asyncio library.

See "UPnP Device Architecture 2.0".

Example of using the Control Point (there is no external dependency):

>>> import asyncio
>>> from pa_dlna.upnp import upnp
>>>
>>> async def main(ipaddr_list):
...   with upnp.UPnPControlPoint(ipaddr_list) as control_point:
...     notification, root_device = await control_point.get_notification()
...     print(f"  Got '{notification}' from {root_device.ip_source}")
...     print(f'  deviceType: {root_device.deviceType}')
...     print(f'  friendlyName: {root_device.friendlyName}')
...     for service in root_device.serviceList.values():
...       print(f'    serviceId: {service.serviceId}')
...
>>>
>>> asyncio.run(main(['192.168.0.254', '192.168.43.83']))
  Got 'alive' from 192.168.0.212
  deviceType: urn:schemas-upnp-org:device:MediaRenderer:1
  friendlyName: Yamaha RN402D
    serviceId: urn:upnp-org:serviceId:AVTransport
    serviceId: urn:upnp-org:serviceId:RenderingControl
    serviceId: urn:upnp-org:serviceId:ConnectionManager
>>>
"""

import asyncio
import logging
import time
import collections
import urllib.parse
import sys
from signal import SIGINT, SIGTERM, strsignal

from . import UPnPError, UPnPFatalError
from .network import parse_ssdp, msearch, notify, http_get
from .xml import (UPnPXMLFatalError, upnp_org_etree, build_etree,
                  xml_of_subelement, findall_childless, scpd_actionlist,
                  scpd_servicestatetable)

logger = logging.getLogger('upnp')

MSEARCH_EVERY = 60                      # send MSEARCH every n seconds

# Fatal error exceptions.
class UPnPControlPointFatalError(UPnPFatalError): pass

# Temporary error exceptions.
class UPnPControlPointError(UPnPError): pass
class UPnPClosedDevice(UPnPError): pass

def shorten(txt, head_len=10, tail_len=5):
    if len(txt) <= head_len + 3 + tail_len:
        return txt
    return txt[:head_len] + '...' + txt[len(txt)-tail_len:]

# Helper class(es).
class AsyncioTasks:
    """Save references to tasks, to avoid tasks being garbage collected.

    See Python github PR 29163 and the corresponding Python issues.
    """

    def __init__(self):
        self._tasks = set()             # references to tasks

    def create_task(self, coro, name):
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._tasks.remove(t))
        return task

    def cancel_all(self):
        for task in self:
            task.cancel()

    def __iter__(self):
        for t in self._tasks:
            yield t

class UPnPQueue:
    """An interruptible asyncio queue.

    UPnPQueue.get() keeps raising the UPnPFatalError exception once an
    instance of this exception has been thrown, whatever the fill state
    of the UPnPQueue.
    Other kinds of exceptions will only be raised once, when the exception is
    thrown while there is a pending call to get().
    """

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.waiter = None
        self.exception = None
        self.buffer = collections.deque()

    def throw(self, exc):
        self.exception = exc
        if self.waiter is not None and not self.waiter.done():
            self.waiter.set_exception(exc)
        elif not isinstance(exc, UPnPFatalError):
            self.exception = None

    def put_nowait(self, item):
        self.buffer.append(item)
        if self.waiter is not None and not self.waiter.done():
            self.waiter.set_result(None)

    async def get(self):
        if self.exception is not None:
            assert isinstance(self.exception, UPnPFatalError)
            raise self.exception

        # Wait for the buffer to be filled.
        if not len(self.buffer):
            if self.waiter is not None:
                raise UPnPControlPointError('Cannot have two tasks'
                                ' simultaneously waiting on an UPnPQueue')
            self.waiter = self.loop.create_future()
            try:
                await self.waiter
            finally:
                self.waiter = None

        if self.exception is not None:
            exc = self.exception
            if not isinstance(exc, UPnPFatalError):
                self.exception = None
            raise exc
        else:
            return self.buffer.popleft()

# Components of an UPnP root device.
class UPnPElement:
    """An UPnP device or service."""

    def __init__(self, root_device):
        self._root_device = root_device
        self._closed = False

    def close(self):
        if not self._closed:
            self._closed = True
            if self._root_device is not None:
                self._root_device.close()

class UPnPService(UPnPElement):
    """An UPnP service.

    Attributes:
      serviceType:  UPnP service type
      serviceId:    Service identifier
      descripion:   the device xml descrition as a string

      actionList:   dict {action name: arguments} where arguments is a dict
                    indexed by the argument name with a value that is another
                    dict whose keys are in.
                    ('name', 'direction', 'relatedStateVariable')

      serviceStateTable: dict {variable name: params} where params is a dict
                    with keys in ('sendEvents', 'multicast', 'dataType',
                    'defaultValue', 'allowedValueList',
                    'allowedValueRange').
                    The value of 'allowedValueList' is a list.
                    The value of 'allowedValueRange' is a dict with keys in
                    ('minimum', 'maximum', 'step').

    Notes:
    Not implemented: Extended data types defined by UPnP Device Architecture
    2.0 are not supported (the 'type' attribute of the 'dataType' subelement
    of a 'stateVariable' element is ignored).

    Not implemented: Unicast and multicast eventing.
    """

    def __init__(self, root_device, attributes):
        super().__init__(root_device)

        # Set the attributes found in the 'service' element of the device
        # description.
        for k, v in attributes.items():
            setattr(self, k, v)
        urlbase = root_device.urlbase
        self.SCPDURL = urllib.parse.urljoin(urlbase, self.SCPDURL)
        self.controlURL = urllib.parse.urljoin(urlbase, self.controlURL)
        if self.eventSubURL is not None:
            self.eventSubURL = urllib.parse.urljoin(urlbase, self.eventSubURL)

        self.actionList = {}
        self.serviceStateTable = {}
        self.description = None
        self._enabled = True
        self._aio_tasks = root_device._aio_tasks

    def close(self):
        if not self._closed:
            super().close()
            self._enabled = False

    async def _run(self):
        description = await http_get(self.SCPDURL)
        self.description = description.decode()

        # Parse the actionList.
        scpd, namespace = upnp_org_etree(self.description)
        self.actionList = scpd_actionlist(scpd, namespace)

        # Parse the serviceStateTable.
        self.serviceStateTable = scpd_servicestatetable(scpd, namespace)

        # Start the control task.
        # XXX
        if self.serviceId == 'urn:upnp-org:serviceId:AVTransport':
            logger.info(f'XXX {self.serviceId}\n {self.serviceStateTable}')

        # Start the eventing task.
        # Not implemented.

        return self

    def _set_enable(self, state=True):
        # A service does not accept soap requests when its '_enabled'
        # attribute is False.
        self._enabled = state

class UPnPDevice(UPnPElement):
    """An UPnP device.

    Attributes:
      descripion:   the device xml description as a string

      All the elements of the 'device' element in the xml description are
      attributes of the UPnPDevice instance. Their value is the value (text)
      of the element except for:

      serviceList:  dict {serviceId value: UPnPService instance}
      deviceList:   dict {deviceType value: UPnPDevice instance}
      iconList:     Not implemented (ignored)
    """

    def __init__(self, root_device):
        super().__init__(root_device)
        self.serviceList = {}
        self.deviceList = {}

    def close(self):
        if not self._closed:
            super().close()
            for service in self.serviceList.values():
                service.close()
            self.serviceList = {}
            for device in self.deviceList.values():
                device.close()
            self.deviceList = {}

    async def _create_services(self, services, namespace, root_device):
        """Create each UPnPService instance with its attributes.

        And await until its xml description has been parsed and the soap task
        started. 'services' is an etree element.
        """

        if services is None:
            return

        for element in services:
            if element.tag != f'{namespace!r}service':
                raise UPnPXMLFatalError(f"Found '{element.tag}' instead"
                                        f" of '{namespace!r}service'")

            d = findall_childless(element, namespace)
            if not d:
                raise UPnPXMLFatalError("Empty 'service' element")
            if 'serviceId' not in d:
                raise UPnPXMLFatalError("Missing 'serviceId' element")

            serviceId = d['serviceId']
            self.serviceList[serviceId] = await (
                                    UPnPService(root_device, d)._run())
            logger.info(f'New service - serviceId: {serviceId}')

    async def _create_devices(self, devices, namespace, root_device):
        """Instantiate the embedded UPnPDevice(s)."""

        if devices is None:
            return

        for element in devices:
            if element.tag != f'{namespace!r}device':
                raise UPnPXMLFatalError(f"Found '{element.tag}' instead"
                                        f" of '{namespace!r}device'")

            d = findall_childless(element, namespace)
            if not d:
                raise UPnPXMLFatalError("Empty 'device' element")
            if 'deviceType' not in d:
                raise UPnPXMLFatalError("Missing 'deviceType' element")

            description = build_etree(element)
            self.deviceList[d['deviceType']] = await (
                  UPnPDevice(root_device)._parse_description(description))

    async def _parse_description(self, description):
        """Parse the xml 'description'.

        Recursively instantiate the tree of embedded devices and their
        services. When this method returns, each UPnPService instance has
        parsed its description and started a task to handle soap requests.
        """

        self.description = description
        device_etree, namespace = upnp_org_etree(description)

        # Add the childless elements of the device element as instance
        # attributes of the UPnPDevice instance.
        d = findall_childless(device_etree, namespace)
        self.__dict__.update(d)
        if not hasattr(self, 'deviceType'):
            raise UPnPXMLFatalError("Missing 'deviceType' element")
        logger.info(f'New device - deviceType: {self.deviceType}')

        root_device = self if self._root_device is None else self._root_device

        services = device_etree.find(f'{namespace!r}serviceList')
        await self._create_services(services, namespace, root_device)

        # Recursion here: _create_devices() calls _parse_description()
        devices = device_etree.find(f'{namespace!r}deviceList')
        await self._create_devices(devices, namespace, root_device)

        return self

    def _set_enable(self, state=True):
        for service in self.serviceList.values():
            service._set_enable(state)
        for device in self.deviceList.values():
            device._set_enable(state)

class UPnPRootDevice(UPnPDevice):
    """An UPnP root device."""

    def __init__(self, control_point, udn, ip_source, location, max_age):
        super().__init__(None)
        self.control_point = control_point  # UPnPControlPoint instance
        self.udn = udn
        self.ip_source = ip_source
        self.location = location
        self._set_valid_until(max_age)
        self.urlbase = None
        self._enabled = True
        self._aio_tasks = AsyncioTasks()

    def close(self):
        if not self._closed:
            super().close()
            self._set_enable(False)
            self._aio_tasks.cancel_all()

            # close() may be called within an exception handler.
            errmsg = f'{self} is closed'
            if sys.exc_info()[0] is None:
                raise UPnPClosedDevice(errmsg)
            else:
                logger.warning(errmsg)

    def _set_enable(self, state=True):
        # Used by the aging process to enable/disable all services and
        # embedded devices.
        self._enabled = state
        super()._set_enable(state)

    def _set_valid_until(self, max_age):
        # The '_valid_until' attribute is the monotonic date when the root
        # device and its services and embedded devices become disabled.
        # '_valid_until' None means no aging is performed.
        if max_age is not None:
            self._valid_until = time.monotonic() + max_age
        else:
            self._valid_until = None

    def _get_timeleft(self):
        if self._valid_until is not None:
            return self._valid_until - time.monotonic()
        return None

    async def _age_root_device(self):
        # Age the root device using SSDP alive notifications.
        while True:
            timeleft = self._get_timeleft()
            if timeleft is not None and timeleft > 0:
                if not self._enabled:
                    self._set_enable(True)
                    logger.info(f'{self} is up')
                await asyncio.sleep(timeleft)
            else:
                # Missing 'CACHE-CONTROL' field in SSDP.
                if timeleft is None:
                    if not self._enabled:
                        self._set_enable(True)
                        logger.info(f'{self} is up')
                elif self._enabled:
                    assert timeleft <= 0
                    self._set_enable(False)
                    logger.info(f'{self} is down')

                # Wake up every second to check for a change in
                # _valid_until.
                await asyncio.sleep(1)

    async def _run(self):
        try:
            description = await http_get(self.location)
            description = description.decode()

            # Find the 'URLBase' subelement (UPnP version 1.1).
            root, namespace = upnp_org_etree(description)
            element = root.find(f'{namespace!r}URLBase')
            self.urlbase = (element.text if element is not None else
                            self.location)

            device_description = xml_of_subelement(description, 'device')
            if device_description is None:
                raise UPnPXMLFatalError("Missing 'device' subelement in root"
                                        ' device description')
            await self._parse_description(device_description)
            self.control_point._put_notification('alive', self)
            await self._age_root_device()
        except UPnPClosedDevice as e:
            logger.warning(f'{e!r}')
        except Exception as e:
            logger.exception(f'{e!r}')
            self.close()
        finally:
            logger.debug(f'End of {self} task')

    def __str__(self):
        """Return a short representation of udn."""
        return f'UPnPRootDevice {shorten(self.udn)}'

# UPnP control point.
class UPnPControlPoint:
    """An UPnP control point."""

    def __init__(self, ip_addresses, ttl=2):
        """Constructor.

        'ip_addresses' list of the local IPv4 addresses of the network
            interfaces where DLNA devices may be discovered.
        'ttl' IP packets time to live.
        """

        if not ip_addresses:
            raise UPnPControlPointFatalError('The list of ip addresses cannot'
                                             ' be empty')
        self.ip_addresses = ip_addresses
        self.ttl = ttl
        self._closed = False
        self._upnp_queue = UPnPQueue()
        self._devices = {}              # {udn: UPnPRootDevice}
        self._aio_tasks = AsyncioTasks()

    def open(self):
        """Start the UPnP Control Point."""

        # Set up signal handlers.
        loop = asyncio.get_running_loop()
        for s in (SIGINT, SIGTERM):
            loop.add_signal_handler(s, lambda s=s: self._sig_handler(s))

        # Start the msearch task.
        self._aio_tasks.create_task(self._ssdp_msearch(), name='ssdp msearch')

        # Start the notify task.
        self._aio_tasks.create_task(self._ssdp_notify(), name='ssdp notify')

    def close(self, exc=None):
        """Close the UPnP Control Point."""

        if not self._closed:
            self._closed = True

            # Raise the exception in the get_notification() method.
            if exc is not None:
                self._upnp_queue.throw(exc)

            for root_device in self._devices.values():
                try:
                    root_device.close()
                except UPnPClosedDevice as e:
                    logger.info(f'{e!r}')

            self._aio_tasks.cancel_all()
            logger.debug('End of upnp task')

    async def get_notification(self):
        """Return the tuple ('alive' or 'byebye', UPnPDevice instance).

        Raise exceptions occuring in the library, including those triggered by
        a KeyboardInterrupt or a SystemExit.
        When the exception is an instance of UPnPFatalError a new call to
        get_notification() will raise the same exception.
        """

        return await self._upnp_queue.get()

    def _put_notification(self, kind, root_device):
        self._upnp_queue.put_nowait((kind, root_device))
        state = 'created' if kind == 'alive' else 'deleted'
        logger.info(f'{root_device} has been {state}')

    def _sig_handler(self, signal):
        errmsg = f'Got signal {strsignal(signal)}'
        logger.info(errmsg)
        if signal == SIGINT:
            self.close(exc=UPnPControlPointFatalError(
                                                f'{KeyboardInterrupt()!r}'))
        else:
            self.close(exc=UPnPControlPointFatalError(
                                                f'{SystemExit()!r}'))

    def _create_root_device(self, header, ip_source):

        # Get the max-age.
        # 'max_age' None means no aging.
        max_age = None
        cache = header.get('CACHE-CONTROL', None)
        if cache is not None:
            age = 'max-age='
            try:
                max_age = int(cache[cache.index(age)+len(age):])
            except ValueError:
                logger.warning(f'Invalid CACHE-CONTROL field in'
                               f' SSDP notify from {ip_source}:\n{header}')
                return

        udn = header['USN'].split('::')[0]
        if udn not in self._devices:
            # Instantiate the UPnPDevice and start its task.
            root_device = UPnPRootDevice(self, udn, ip_source,
                                         header['LOCATION'], max_age)
            self._aio_tasks.create_task(root_device._run(),
                                       name=str(root_device))
            self._devices[udn] = root_device
            logger.info(f'New {root_device} at {ip_source}')

        else:
            root_device = self._devices[udn]

            # Avoid cluttering the logs when the aging refresh occurs within 5
            # seconds of the last one, assuming all max ages are the same.
            timeleft = root_device._get_timeleft()
            if (timeleft is not None and
                    max_age is not None and
                    max_age - timeleft > 5):
                logger.debug(f'Refresh with max-age={max_age}'
                             f' for {root_device}')

            # Refresh the aging time.
            root_device._set_valid_until(max_age)

    def _process_ssdp(self, datagram, ip_source, is_msearch):
        """Process the received datagrams.

        'is_msearch' is True when processing a msearch response, otherwise it
        is a notify advertisement.
        """

        header = parse_ssdp(datagram, ip_source, is_msearch)
        if header is None:
            return

        msg = 'msearch response' if is_msearch else 'notify advertisement'
        logger.debug(f'Got {msg} from {ip_source}')

        if is_msearch or (header['NTS'] == 'ssdp:alive'):
            self._create_root_device(header, ip_source)
        else:
            nts = header['NTS']
            if nts == 'ssdp:byebye':
                udn = header['USN'].split('::')[0]
                root_device = self._devices.get(udn, None)
                if root_device is not None:
                    root_device.close()
                    self._put_notification('byebye', root_device)
                    del self._devices[udn]

            elif nts == 'ssdp:update':
                logger.warning(f'Ignore not supported {nts} notification'
                               f' from {ip_source}')

            else:
                logger.warning(f"Unknown NTS field '{nts}' in SSDP notify"
                               ' from {ip_source}')

    async def _ssdp_msearch(self):
        """Send msearch multicast SSDPs and process unicast responses."""

        try:
            while True:
                for ip in self.ip_addresses:
                    result = await msearch(ip, self.ttl)
                    for (datagram, src_addr) in result:
                        self._process_ssdp(datagram, src_addr[0],
                                           is_msearch=True)
                await asyncio.sleep(MSEARCH_EVERY)
        except Exception as e:
            exc = f'{e!r}'
            logger.exception(exc)
            self.close(exc=UPnPControlPointFatalError(exc))

    async def _ssdp_notify(self):
        """Listen to SSDP notifications."""

        try:
            await notify(self.ip_addresses, self._process_ssdp)
        except Exception as e:
            exc = f'{e!r}'
            logger.exception(exc)
            self.close(exc=UPnPControlPointFatalError(exc))

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
