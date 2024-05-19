"""libpulse_ctypes test cases."""

import io
from unittest import TestCase, mock
from contextlib import redirect_stdout

# Load the tests in the order they are declared.
from ...tests import requires_resources
from ...upnp.tests import load_ordered_tests

import pa_dlna.libpulse.libpulse_ctypes as libpulse_ctypes_module
from ..libpulse_ctypes import PulseCTypes, PulseCTypesLibError

@requires_resources('libpulse')
class LibPulseCTypesTestCase(TestCase):
    def test_print_types(self):
        with redirect_stdout(io.StringIO()) as output:
            libpulse_ctypes_module.print_types(['types', 'structs',
                                                'callbacks', 'prototypes'])
        output = output.getvalue()
        self.assertIn('pa_io_event_flags_t', output)
        self.assertIn('pa_server_info', output)
        self.assertIn('io_new', output)
        self.assertIn('pa_context_new', output)

    def test_missing_lib(self):
        with mock.patch.object(libpulse_ctypes_module,
                               'find_library') as find_library,\
                self.assertRaises(PulseCTypesLibError):
            find_library.return_value = None
            PulseCTypes()
