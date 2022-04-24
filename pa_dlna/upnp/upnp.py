"""A basic UPnP Control Point asyncio library.

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

The API is made of the methods and attibutes of the UPnPControlPoint,
UPnPRootDevice, UPnPDevice and UPnPService classes.
See "UPnP Device Architecture 2.0".

Not implemented:

- The extended data types in the service xml description - see
  "2.5.1 Defining and processing extended data types" in UPnP 2.0.

- SOAP <Header> elements are ignored - see "3.1.1 SOAP Profile" in UPnP 2.0.

- Unicast and multicast eventing are not implemented.
"""

import asyncio
import logging
import time
import collections
import urllib.parse
from signal import SIGINT, SIGTERM, strsignal

from . import UPnPError
from .network import parse_ssdp, msearch, notify, http_get
from .xml import (UPnPXMLFatalError, upnp_org_etree, build_etree,
                  xml_of_subelement, findall_childless, scpd_actionlist,
                  scpd_servicestatetable)

logger = logging.getLogger('upnp')

MSEARCH_EVERY = 60                      # send MSEARCH every n seconds
ICON_ELEMENTS = ('mimetype', 'width', 'height', 'depth', 'url')

class UPnPControlPointError(UPnPError): pass

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

# Components of an UPnP root device.
Icon = collections.namedtuple('Icon', ICON_ELEMENTS)
class UPnPElement:
    """An UPnP device or service."""

    def __init__(self, root_device):
        self._root_device = root_device

    def closed(self):
        root_device = self if self._root_device is None else self._root_device
        return root_device._closed

class UPnPService(UPnPElement):
    """An UPnP service.

    Attributes:
      serviceType   UPnP service type
      serviceId     Service identifier
      description   the device xml descrition as a string

      actionList    dict {action name: arguments} where arguments is a dict
                    indexed by the argument name with a value that is another
                    dict whose keys are in.
                    ('name', 'direction', 'relatedStateVariable')

      serviceStateTable  dict {variable name: params} where params is a dict
                    with keys in ('sendEvents', 'multicast', 'dataType',
                    'defaultValue', 'allowedValueList',
                    'allowedValueRange').
                    The value of 'allowedValueList' is a list.
                    The value of 'allowedValueRange' is a dict with keys in
                    ('minimum', 'maximum', 'step').

    Methods:
      closed        return True if the root device is closed
      soap_action   coroutine - XXX
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
        self._aio_tasks = root_device._aio_tasks

    async def _run(self):
        description = await http_get(self.SCPDURL)
        self.description = description.decode()

        # Parse the actionList.
        scpd, namespace = upnp_org_etree(self.description)
        self.actionList = scpd_actionlist(scpd, namespace)

        # Parse the serviceStateTable.
        self.serviceStateTable = scpd_servicestatetable(scpd, namespace)

        # Start the eventing task.
        # Not implemented.

        return self

class UPnPDevice(UPnPElement):
    """An UPnP device.

    Attributes:
      description   the device xml description as a string
      urlbase       the url used to retrieve the description of the root
                    device or the 'URLBase' element (deprecated from UPnP 1.1
                    onwards)

      All the subelements of the 'device' element in the xml description are
      attributes of the UPnPDevice instance: 'deviceType', 'friendlyName',
      'manufacturer', 'UDN', etc... (see the specification).
      Their value is the value (text) of the element except for:

      serviceList   dict {serviceId value: UPnPService instance}
      deviceList    dict {deviceType value: UPnPDevice instance}
      iconList      list of instances of the Icon namedtuple; use 'urlbase'
                    and the (relative) 'url' attribute of the namedtuple to
                    retrieve the icon.

    Methods:
      closed        return True if the root device is closed
    """

    def __init__(self, root_device):
        super().__init__(root_device)
        self.description = None
        self.urlbase = None
        if root_device is not None:
            self.urlbase = root_device.urlbase

        self.serviceList = {}
        self.deviceList = {}
        self.iconList = []

    def _create_icons(self, icons, namespace):
        if icons is None:
            return

        for element in icons:
            if element.tag != f'{namespace!r}icon':
                raise UPnPXMLFatalError(f"Found '{element.tag}' instead"
                                        f" of '{namespace!r}icon'")

            d = findall_childless(element, namespace)
            if not d:
                raise UPnPXMLFatalError("Empty 'icon' element")
            if all(d.get(tag) for tag in ICON_ELEMENTS):
                self.iconList.append(Icon(**d))
            else:
                logger.warning("Missing required subelement of 'icon' in"
                               ' device description')

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

        icons = device_etree.find(f'{namespace!r}iconList')
        self._create_icons(icons, namespace)

        services = device_etree.find(f'{namespace!r}serviceList')
        await self._create_services(services, namespace, root_device)

        # Recursion here: _create_devices() calls _parse_description()
        devices = device_etree.find(f'{namespace!r}deviceList')
        await self._create_devices(devices, namespace, root_device)

        return self

class UPnPRootDevice(UPnPDevice):
    """An UPnP root device.

    An UpNP root device is also an UPnPDevice, see the UPnPDevice __doc__ for
    the other attributes and methods available.

    Attributes:
      control_point  UPnPControlPoint instance
      ip_source     IP source address of the UPnP device
      location      'Location' field value in the header of the SSDP notify
                    or msearch

    Methods:
      close         close the root device
    """

    def __init__(self, control_point, udn, ip_source, location, max_age):
        super().__init__(None)
        self.control_point = control_point  # UPnPControlPoint instance
        self._udn = udn
        self.ip_source = ip_source
        self.location = location
        self._set_valid_until(max_age)

        self._closed = True
        self._task = None
        self._aio_tasks = AsyncioTasks()

    def close(self):
        """Close the root device.

        Close the root device its services and recursively all its embedded
        devices and their services.
        """

        if not self._closed:
            self._closed = True
            if self._task is not None:
                self._task.cancel()
            self._aio_tasks.cancel_all()
            logger.warning(f'{self} is closed')
            self.control_point._remove_root_device(self._udn)

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
            # Missing or invalid 'CACHE-CONTROL' field in SSDP.
            # Wait for a change in _valid_until.
            if timeleft is None:
                await asyncio.sleep(60)
            elif timeleft > 0:
                await asyncio.sleep(timeleft)
            else:
                logger.warning(f'Aging expired on {self}')
                self.close()
                break

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
            self._closed = False
            self.control_point._put_notification('alive', self)
            await self._age_root_device()
        except Exception as e:
            logger.exception(f'{e!r}')
            self.close()
        finally:
            logger.debug(f'End of {self} task')

    def __str__(self):
        """Return a short representation of _udn."""

        return f'UPnPRootDevice {shorten(self._udn)}'

# UPnP control point.
class UPnPControlPoint:
    """An UPnP control point.

    Once an UPnPControlPoint has been opened, either with the open() method or
    as a context manager, the coroutine that has opened the control point must
    be ready to handle asyncio.CancelledError until the control point is
    closed. The CancelledError message contains the exception that had been
    raised in the library, it may be for example a KeyboardInterrupt.

    Attributes:
      ip_addresses  list of local IPv4 addresses of the network interfaces
                    where UPnP devices may be discovered
      ttl           the IP packets time to live

    Methods:
      open          coroutine - start the UPnP Control Point
      close         close the UPnP Control Point
      get_notification: coroutine - return a notification and the
                    corresponding UPnPRootDevice instance
      __aenter__    UPnPControlPoint is also an asynchronous context manager
      __aclose__
    """

    def __init__(self, ip_addresses, ttl=2):
        """Constructor.

        'ip_addresses' list of the local IPv4 addresses of the network
            interfaces where DLNA devices may be discovered.
        'ttl' IP packets time to live.
        """

        if not ip_addresses:
            raise UPnPControlPointError('The list of ip addresses cannot'
                                             ' be empty')
        self.ip_addresses = ip_addresses
        self.ttl = ttl
        self._closed = False
        self._upnp_queue = asyncio.Queue()
        self._devices = {}              # {udn: UPnPRootDevice}
        self._curtask = None            # task running UPnPControlPoint.open()
        self._aio_tasks = AsyncioTasks()

    async def open(self):
        """Start the UPnP Control Point."""

        # Get the caller's task.
        # open() being a coroutine ensures that it is run by a task or a
        # coroutine with a task.
        self._curtask = asyncio.current_task()

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

            # Remove the signal handlers.
            loop = asyncio.get_running_loop()
            for s in (SIGINT, SIGTERM):
                loop.remove_signal_handler(s)

            for root_device in list(self._devices.values()):
                root_device.close()

            if self._curtask is not None:
                errmsg = f'{exc!r}' if exc else None
                self._curtask.cancel(msg=errmsg)
                self._curtask = None

            self._aio_tasks.cancel_all()
            logger.debug('UPnPControlPoint is closed')

    async def get_notification(self):
        """Return the tuple ('alive' or 'byebye', UPnPRootDevice instance)."""

        return await self._upnp_queue.get()

    def _put_notification(self, kind, root_device):
        self._upnp_queue.put_nowait((kind, root_device))
        state = 'created' if kind == 'alive' else 'deleted'
        logger.info(f'{root_device} has been {state}')

    def _sig_handler(self, signal):
        errmsg = f'Got signal {strsignal(signal)}'
        logger.info(errmsg)
        if signal == SIGINT:
            self.close(exc=KeyboardInterrupt())
        else:
            self.close(exc=SystemExit())

    def _create_root_device(self, header, ip_source):
        # Get the max-age.
        # 'max_age' None means no aging.
        max_age = None
        cache = header.get('CACHE-CONTROL')
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
            task = self._aio_tasks.create_task(root_device._run(),
                                               name=str(root_device))
            root_device._task = task
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

    def _remove_root_device(self, udn):
        root_device = self._devices.get(udn)
        if root_device is not None:
            root_device.close()
            self._put_notification('byebye', root_device)
            del self._devices[udn]

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
                self._remove_root_device(udn)

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
            logger.exception(f'{e!r}')
            self.close(exc=e)

    async def _ssdp_notify(self):
        """Listen to SSDP notifications."""

        try:
            await notify(self.ip_addresses, self._process_ssdp)
        except Exception as e:
            logger.exception(f'{e!r}')
            self.close(exc=e)

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.close()
