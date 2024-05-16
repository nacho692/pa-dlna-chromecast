"""libpulse_ctypes test cases."""

from unittest import TestCase, mock

# Load the tests in the order they are declared.
from ...upnp.tests import load_ordered_tests

import pa_dlna.libpulse.libpulse_ctypes as libpulse_ctypes_module
from ..libpulse_ctypes import PulseCTypes, PulseCTypesLibError

class LibPulseCTypesTestCase(TestCase):
    def test_missing_lib(self):
        with mock.patch.object(libpulse_ctypes_module,
                               'find_library') as find_library,\
                self.assertRaises(PulseCTypesLibError):
            find_library.return_value = None
            PulseCTypes()
