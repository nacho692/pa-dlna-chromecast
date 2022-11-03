import sys
import subprocess
import shutil
import logging

from .upnp import NL_INDENT

logger = logging.getLogger('encoder')

DEFAULT_SELECTION = (
    # Lossless encoders.
    'FFMpegFlacEncoder',
    'L16Encoder',

    # Lossy encoders.
    'FFMpegOpusEncoder',
    'FFMpegVorbisEncoder',
    'FFMpegMp3Encoder',
    'FFMpegAacEncoder',
)

def select_encoder(config, renderer_name, pinfo, udn):
    """Select the encoder.

    Return the selected encoder, the mime type and protocol info.
    """

    def available(*, udns):
        encoders = []
        for section, instance in config.items():
            in_list = (section not in config.encoder_list if udns else
                       section in config.encoder_list)
            if in_list and instance.available:
                encoders.append((section, instance))
        return encoders

    protocol_infos = [x for x in pinfo['Sink'].split(',')]
    mime_types = [proto.split(':')[2] for proto in protocol_infos]
    logger.debug(f'{renderer_name} renderer mime types:' + NL_INDENT +
                 f'{mime_types}')

    # Try first the configured udns.
    for section, encoder in available(udns=True):
        if section == udn:
            # Check that the list of mime_types holds one of the  mime types
            # supported by this encoder and return the encoder and this mime
            # type.
            for idx, mime_type in enumerate(mime_types):
                if encoder.has_mime_type(mime_type):
                    return encoder, encoder.mime_type, protocol_infos[idx]
            else:
                logger.error(f'No matching mime type for the udn configured'
                             f' on the {encoder} encoder')
                return None

    # Then the encoders proper.
    for _, encoder in available(udns=False):
        for idx, mime_type in enumerate(mime_types):
            if encoder.has_mime_type(mime_type):
                return encoder, encoder.mime_type, protocol_infos[idx]

class Encoder:
    """The pa-dlna default configuration.

    This is the built-in pa-dlna configuration written as a text. It can be
    parsed by a Python Configuration parser and consists of sections, each led
    by a [section] header, followed by option/value entries separated by
    '='. See https://docs.python.org/3/library/configparser.html.

    A section is either [DEFAULT] or [EncoderName]. The options defined in the
    [DEFAULT] section apply to all the other sections and are overriden when
    also defined in an [EncoderName] section.

    The options defined in the pa-dlna.conf user configuration file (also
    parseable by a Python Configuration parser) override the options of the
    default configuration listed here. The pa-dlna.conf file also allows the
    specification of options per device, see the pa-dlna documentation.

    The 'selection' option is an ordered comma separated list of
    encoders. This list is used to select the first encoder matching one of
    the mime-types supported by a discovered DLNA device when there is no
    specific configuration for the given device.

    Notes:
    The 'selection' option is written as a multi-line in which case all the
    lines after the first line start with a white space.

    The default value of 'selection' lists first the lossless encoders and
    then the lossy ones.
    See https://trac.ffmpeg.org/wiki/Encode/HighQualityAudio.
    """

    def __init__(self):
        endianess = sys.byteorder
        self._pulse_format = 's16le' if endianess == 'little' else 's16be'
        self.selection = DEFAULT_SELECTION
        self.rate = 44100
        self.channels = 2
        self.metadata = True

    @property
    def available(self):
        return self._available

    @property
    def mime_type(self):
        raise NotImplementedError

    def has_mime_type(self, mime_type):
        raise NotImplementedError

    @property
    def command(self):
        return self._command()

    def __str__(self):
        return self.__class__.__name__

ROOT_ENCODER = Encoder

class StandAloneEncoder(Encoder):
    """Abstract class for standalone encoders."""

class L16Encoder(StandAloneEncoder):
    """L16 encoder.

    To play and check the result obtained using a TestRenderer with the
    '--renderers audio/L16\;rate=44100\;channels=2' command line argument,
    one may use the 'ffplay' tool from ffmpeg and run the command:

        $ ffplay -f s16be -ac 2 -ar 44100 output_file

    Note that the ';' character must be escaped on the command line or the
    value of the '--renderers' option must be quoted.

    See also https://datatracker.ietf.org/doc/html/rfc2586.
    """

    def __init__(self):
        self._available = True
        self._mime_types = ['audio/l16']
        self._network_format = 's16be'
        super().__init__()

    @property
    def mime_type(self):
        assert hasattr(self, 'requested_mtype')
        return self.requested_mtype

    def has_mime_type(self, mime_type):
        # For example 'audio/L16;rate=44100;channels=2'.
        mtype = [p.strip() for p in mime_type.lower().split(';')]

        if mtype[0] != self._mime_types[0]:
            return False

        rate_channels = [0, 0]          # list of [rate, channels]
        for param in mtype[1:]:
            for (n, prefix) in enumerate(['rate=', 'channels=']):
                if param.startswith(prefix):
                    try:
                        rate_channels[n] = int(param[len(prefix):])
                    except ValueError:
                        return False
                    break

        # The rate parameter is required.
        if rate_channels[0] == self.rate:
            if rate_channels[1] in (0, self.channels):
                self.requested_mtype = mime_type
                return True


class FFMpegEncoder(Encoder):
    """Abstract class for ffmpeg encoders.

    See also https://www.ffmpeg.org/ffmpeg.html.
    """

    PGM = None
    ENCODERS = None

    def __init__(self, mime_types, codec, file_format=None, encoder=None):
        if self.ENCODERS is None:
            FFMpegEncoder.ENCODERS = ''
            FFMpegEncoder.PGM = shutil.which('ffmpeg')
            if self.PGM is not None:
                proc = subprocess.run([self.PGM, '-encoders'],
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True)
                FFMpegEncoder.ENCODERS = proc.stdout
        self._available = codec in self.ENCODERS
        self._pgm = self.PGM
        self._mime_types = mime_types
        # End of setting options as comments.

        super().__init__()
        file_format = codec if file_format is None else file_format
        self.args = (f'-loglevel error -hide_banner -nostats'
                     f' -ac {self.channels} -ar {self.rate}'
                     f' -f {self._pulse_format} -i -'
                     f' -f {file_format}')
        if encoder is not None:
            self.args += f' -c:a {encoder}'

    @property
    def mime_type(self):
        assert hasattr(self, 'requested_mtype')
        return self.requested_mtype

    def has_mime_type(self, mime_type):
        if mime_type.lower() in self._mime_types:
            self.requested_mtype = mime_type
            return True

    def add_args(self, cmd):
        return cmd

    def _command(self):
        cmd = [self._pgm]
        cmd.extend(self.args.split())
        cmd = self.add_args(cmd)
        cmd.append('pipe:1')
        return cmd

class FFMpegAacEncoder(FFMpegEncoder):
    """Aac encoder.

    'bitrate' is expressed in kilobits.
    See also https://trac.ffmpeg.org/wiki/Encode/AAC.
    """

    def __init__(self):
        super().__init__(['audio/aac', 'audio/x-aac'], 'aac',
                         file_format='adts', encoder='aac')
        self.bitrate = 192

    def add_args(self, cmd):
        cmd.extend(['-b:a', f'{self.bitrate}k'])
        return cmd

class FFMpegFlacEncoder(FFMpegEncoder):
    """Flac encoder.

    See also https://ffmpeg.org/ffmpeg-all.html#flac-2.
    """

    def __init__(self):
        super().__init__(['audio/flac', 'audio/x-flac'], 'flac')

class FFMpegMp3Encoder(FFMpegEncoder):
    """Mp3 encoder.

    Setting 'bitrate' to 0 causes VBR encoding to be chosen and 'qscale'
    to be used instead, otherwise 'bitrate' is expressed in kilobits.
    See also https://trac.ffmpeg.org/wiki/Encode/MP3.
    """

    def __init__(self):
        super().__init__(['audio/mp3', 'audio/mpeg'], 'mp3',
                         encoder='libmp3lame')
        self.bitrate = 256
        self.qscale = 2

    def add_args(self, cmd):
        if self.bitrate != 0:
            cmd.extend(['-b:a', f'{self.bitrate}k'])
        else:
            cmd.extend(['-qscale:a', str(self.qscale)])
        return cmd

class FFMpegOpusEncoder(FFMpegEncoder):
    """Opus encoder.

    See also https://wiki.xiph.org/Opus_Recommended_Settings.
    """

    def __init__(self):
        super().__init__(['audio/opus', 'audio/x-opus'], 'opus',
                         encoder='libopus')
        self.bitrate = 128

    def add_args(self, cmd):
        cmd.extend(['-b:a', f'{self.bitrate}k'])
        return cmd

class FFMpegVorbisEncoder(FFMpegEncoder):
    """Vorbis encoder.

    Setting 'bitrate' to 0 causes VBR encoding to be chosen and 'qscale'
    to be used instead, otherwise 'bitrate' is expressed in kilobits.
    See also https://ffmpeg.org/ffmpeg-all.html#libvorbis.
    """

    def __init__(self):
        super().__init__(['audio/vorbis', 'audio/x-vorbis'], 'vorbis',
                         file_format='ogg', encoder='libvorbis')
        self.bitrate = 256
        self.qscale = 3.0

    def add_args(self, cmd):
        if self.bitrate != 0:
            cmd.extend(['-b:a', f'{self.bitrate}k'])
        else:
            cmd.extend(['-qscale:a', str(self.qscale)])
        return cmd
