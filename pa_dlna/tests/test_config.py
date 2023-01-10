"""Encoders configuration test cases."""

import io
from unittest import mock
from contextlib import redirect_stdout
from configparser import ParsingError

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import BaseTestCase, requires_resources

class Encoder:
    def __init__(self):
        self.selection = ['TestEncoder']
        self.args = None
        self.option = 1

    def set_args(self):
        return str(self.args) == 'None'

class StandAloneEncoder(Encoder):
    def __init__(self):
        super().__init__()

class TestEncoder(StandAloneEncoder):
    def __init__(self):
        StandAloneEncoder.__init__(self)

class encoders_module:
    def __init__(self, root=Encoder, encoder=TestEncoder):
        self.ROOT_ENCODER = root
        self.TestEncoder = encoder

@requires_resources('os.devnull')
class DefaultConfig(BaseTestCase):
    """Default configuration tests."""

    def setUp(self):
        super().setUp()

    def test_invalid_section(self):
        name = 'InvalidEncoder'
        class _Encoder(Encoder):
            def __init__(self):
                super().__init__()
                self.selection = [name]

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module(root=_Encoder)),\
                self.assertRaises(ParsingError) as cm:
            from ..config import DefaultConfig
            DefaultConfig()

        self.assertEqual(cm.exception.args[0],
                         f"'{name}' is not a valid class name")

    def tearDown(self):
        super().tearDown()

@requires_resources('os.devnull')
class UserConfig(BaseTestCase):
    """User configuration tests."""

    def setUp(self):
        super().setUp()

    def test_invalid_value(self):
        value = 'string'
        PA_DLNA_CONF = f"""
        [TestEncoder]
        option = {value}
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=PA_DLNA_CONF)),\
                self.assertRaises(ParsingError) as cm:
            from ..config import UserConfig
            UserConfig()

        self.assertRegex(cm.exception.args[0],
                         f"TestEncoder.option: invalid .*'{value}'")

    def test_invalid_option(self):
        PA_DLNA_CONF = """
        [TestEncoder]
        invalid = 1
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=PA_DLNA_CONF)),\
                self.assertRaises(ParsingError) as cm:
            from ..config import UserConfig
            UserConfig()

        self.assertEqual(cm.exception.args[0],
                         "Unknown option 'TestEncoder.invalid'")

    def test_no_userfile(self):
        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open()) as m_open:
            m_open.side_effect = FileNotFoundError()
            from ..config import UserConfig
            cfg = UserConfig()

        self.assertEqual(cfg.encoders['TestEncoder'].__dict__,
                         {'args': 'None', 'option': 1})

    def test_not_available(self):
        class UnAvailableEncoder(StandAloneEncoder):
            def __init__(self):
                super().__init__()
                self._available = False

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module(encoder=UnAvailableEncoder)),\
                mock.patch('builtins.open', mock.mock_open()) as m_open,\
                redirect_stdout(io.StringIO()) as output:
            m_open.side_effect = FileNotFoundError()
            from ..config import UserConfig
            cfg = UserConfig()
            cfg.print_internal_config()

        self.assertEqual(cfg.encoders, {})
        self.assertIn('No encoder is available\n', output.getvalue())

    def test_invalid_section(self):
        PA_DLNA_CONF = """
        [TestEncoder.]
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=PA_DLNA_CONF)),\
                self.assertRaises(ParsingError) as cm:
            from ..config import UserConfig
            UserConfig()

        self.assertEqual(cm.exception.args[0],
                         "'TestEncoder.' is not a valid section")

    def test_not_exists(self):
        PA_DLNA_CONF = """
        [DEFAULT]
        selection = UnknownEncoder
        [UnknownEncoder]
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=PA_DLNA_CONF)),\
                self.assertRaises(ParsingError) as cm:
            from ..config import UserConfig
            UserConfig()

        self.assertEqual(cm.exception.args[0]
                         , "'UnknownEncoder' encoder does not exist")

    def test_unvalid_encoder(self):
        PA_DLNA_CONF = """
        [DEFAULT]
        selection = UnknownEncoder
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=PA_DLNA_CONF)),\
                self.assertRaises(ParsingError) as cm:
            from ..config import UserConfig
            UserConfig()

        self.assertEqual(cm.exception.args[0], "'UnknownEncoder' in the"
                         ' selection is not a valid encoder')

    def test_udn_section(self):
        udn = 'uuid:9ab0c000-f668-11de-9976-00a0de98381a'
        PA_DLNA_CONF = f"""
        [TestEncoder.{udn}]
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=PA_DLNA_CONF)),\
                redirect_stdout(io.StringIO()) as output:
            from ..config import UserConfig
            UserConfig().print_internal_config()

        self.assertIn(f"{{'{udn}': {{'_encoder': 'TestEncoder'",
                      output.getvalue())

    def tearDown(self):
        super().tearDown()

if __name__ == '__main__':
    unittest.main(verbosity=2)
