# UPnP exceptions.
class UPnPError(Exception): pass

# All exported objects.
from .upnp import (UPnPClosedDeviceError, UPnPInvalidSoapError,
                   UPnPSoapFaultError,
                   UPnPControlPoint, UPnPRootDevice, UPnPDevice, UPnPService)
from .xml import UPnPXMLError, pprint_xml
