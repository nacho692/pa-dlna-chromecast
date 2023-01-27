# UPnP exceptions.
class UPnPError(Exception): pass

# TEST_LOGLEVEL is below logging.DEBUG and is only used by the test suite.
TEST_LOGLEVEL = 5

# All exported objects.
from .upnp import (UPnPClosedDeviceError, UPnPInvalidSoapError,
                   UPnPSoapFaultError,
                   UPnPControlPoint, UPnPRootDevice, UPnPDevice, UPnPService)
from .network import ipv4_addresses
from .util import (NL_INDENT, shorten, log_exception, AsyncioTasks,
                   log_exception)
from .xml import UPnPXMLError, pformat_xml
