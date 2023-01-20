"""Encoders configuration test cases."""

import io
from unittest import mock
from contextlib import redirect_stdout
from configparser import ParsingError

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import BaseTestCase, requires_resources

UDN = 'uuid:ffffffff-ffff-ffff-ffff-ffffffffffff'

class Encoder:
    def __init__(self):
        self.selection = ['TestEncoder']
        self.args = None
        self.option = 1

    @property
    def available(self):
        if hasattr(self, '_available'):
            return self._available
        return True

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

@requires_resources('os.devnull')
class UserConfig(BaseTestCase):
    """User configuration tests."""

    def test_invalid_value(self):
        value = 'string'
        pa_dlna_conf = f"""
        [TestEncoder]
          option = {value}
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)),\
                self.assertRaises(ParsingError) as cm:
            from ..config import UserConfig
            UserConfig()

        self.assertRegex(cm.exception.args[0],
                         f"TestEncoder.option: invalid .*'{value}'")

    def test_invalid_option(self):
        pa_dlna_conf = """
        [TestEncoder]
          invalid = 1
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)),\
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
        pa_dlna_conf = """
        [TestEncoder.]
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)),\
                self.assertRaises(ParsingError) as cm:
            from ..config import UserConfig
            UserConfig()

        self.assertEqual(cm.exception.args[0],
                         "'TestEncoder.' is not a valid section")

    def test_not_exists(self):
        pa_dlna_conf = """
        [DEFAULT]
          selection = UnknownEncoder
        [UnknownEncoder]
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)),\
                self.assertRaises(ParsingError) as cm:
            from ..config import UserConfig
            UserConfig()

        self.assertEqual(cm.exception.args[0]
                         , "'UnknownEncoder' encoder does not exist")

    def test_unvalid_encoder(self):
        pa_dlna_conf = """
        [DEFAULT]
          selection = UnknownEncoder
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)),\
                self.assertRaises(ParsingError) as cm:
            from ..config import UserConfig
            UserConfig()

        self.assertEqual(cm.exception.args[0], "'UnknownEncoder' in the"
                         ' selection is not a valid encoder')

    def test_udn_section(self):
        pa_dlna_conf = f"""
        [TestEncoder.{UDN}]
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)),\
                redirect_stdout(io.StringIO()) as output:
            from ..config import UserConfig
            UserConfig().print_internal_config()

        self.assertIn(f"{{'{UDN}': {{'_encoder': 'TestEncoder'",
                      output.getvalue())

@requires_resources('os.devnull')
class Encoders(BaseTestCase):
    """Encoders tests."""

    def l16_mime_type(self, mime_type, rate=0, channels=0, udn=None):

        pinfo = {'Sink': f'http-get:*:{mime_type}:DLNA.ORG_PN=LPCM'}
        from ..encoders import select_encoder
        from ..config import UserConfig
        config = UserConfig()

        if udn is not None:
            self.assertEqual(config.encoders, {})
            self.assertIn(udn, config.udns)
        else:
            # Set the attributes of the L16Encoder instance.
            l16 = config.encoders['L16Encoder']
            l16.rate = rate
            l16.channels = channels

        res = select_encoder(config, 'Renderer name', pinfo, udn)

        if res is not None:
            encoder, mtype, protocol_info = res
            self.assertEqual(encoder.__class__.__name__, 'L16Encoder')
            self.assertEqual(mtype, mime_type)
            self.assertEqual(protocol_info,
                             f'http-get:*:{mime_type}:DLNA.ORG_PN=LPCM')

        return res

    def test_select_L16(self):
        rate_channels = [(44100, 2),
                         (44100, 1),
                         (88200, 2)]
        mime_types = ['audio/L16;channels={channels};rate={rate}',
                      'audio/l16;rate={rate};channels={channels}']
        for rate, channels in rate_channels:
            for mtype in mime_types:
                mtype = mtype.format(rate=rate, channels=channels)
                with self.subTest(mtype=mtype),\
                     mock.patch('builtins.open', mock.mock_open()) as m_open:
                    m_open.side_effect = FileNotFoundError()
                    self.l16_mime_type(mtype, rate, channels)

    def test_select_udn(self):
        pa_dlna_conf = f"""
        [DEFAULT]
          selection =
        [L16Encoder.{UDN}]
        """

        with mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)):
            res = self.l16_mime_type('audio/L16;channels=2;rate=44100',
                                     udn=UDN)
            self.assertNotEqual(res, None)

    def test_bad_mtype(self):
        pa_dlna_conf = f"""
        [DEFAULT]
          selection =
        [L16Encoder.{UDN}]
        """

        mime_types = [
            'audio/L16;channels=2;rate=88200',  # 88200 is invalid param
            'audio/FOO;channels=2;rate=44100',  # not L16 mime type
            'audio/L16;channels=2;rate=FOO'     # wrong param value
        ]

        for mtype in mime_types:
            with self.subTest(mtype=mtype),\
                    mock.patch('builtins.open', mock.mock_open(
                        read_data=pa_dlna_conf)):
                res = self.l16_mime_type(mtype, udn=UDN)
                self.assertEqual(res, None)

if __name__ == '__main__':
    unittest.main(verbosity=2)
