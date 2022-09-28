# UPnP exceptions.
class UPnPError(Exception): pass

# All exported objects.
from .upnp import (UPnPControlPointError, UPnPClosedDeviceError,
                   UPnPInvalidSoapError, UPnPSoapFaultError,
                   UPnPControlPoint, UPnPRootDevice, UPnPDevice, UPnPService,
                   AsyncioTasks, NL_INDENT, shorten)
from .network import (UPnPInvalidSsdpError, UPnPInvalidHttpError)
from .xml import (UPnPXMLError, pprint_xml)
