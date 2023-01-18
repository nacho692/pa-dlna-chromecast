# UPnP exceptions.
class UPnPError(Exception): pass

# TEST_LOGLEVEL is below logging.DEBUG and is only used by the test suite.
TEST_LOGLEVEL = 5

# All exported objects.
from .upnp import (UPnPClosedDeviceError, UPnPInvalidSoapError,
                   UPnPSoapFaultError,
                   UPnPControlPoint, UPnPRootDevice, UPnPDevice, UPnPService)
from .xml import UPnPXMLError, pprint_xml
