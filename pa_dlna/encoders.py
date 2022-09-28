import sys
import subprocess
import shutil
import logging

logger = logging.getLogger('encoder')

DEFAULT_CONFIG = (
    # Lossless encoders.
    'FFMpegFlacEncoder',
    'L16Encoder',

    # Lossy encoders.
    'FFMpegOpusEncoder',
    'FFMpegVorbisEncoder',
    'FFMpegMp3Encoder',
    'FFMpegAacEncoder',
)

def select_encoder(encoders, mime_types, udn):
    """Select the encoder.

    Return the selected encoder and the mime type.
    """

    def available_encoders(encoders):
        return (instance for instance in encoders.values()
                if instance.available)

    for encoder in available_encoders(encoders):
        if udn in (u.strip() for u in encoder.udns.split(',')):
            # Check that the list of mime_types holds one of the  mime types
            # supported by this encoder and return the encoder and this mime
            # type.
            for mime_type in mime_types:
                if encoder.has_mime_type(mime_type.lower()):
                    return encoder, encoder.mime_type
            else:
                logger.error(f'No matching mime type for the udn configured'
                             f' on the {encoder} encoder')
                return None

    for encoder in available_encoders(encoders):
        for mime_type in mime_types:
            if encoder.has_mime_type(mime_type.lower()):
                return encoder, encoder.mime_type

class Encoder:
    """INI configuration file for pa-dlna.

    This file is used to find an encoder matching one of the mime-types
    supported by a discovered DLNA device. The selection is made as follows:

    1) Use the first encoder whose 'udns' option holds the UDN (Unique Device
       Name) of the device, 'udns' is a comma separated list of UDNs.
       An UDN value has the format 'uuid:UUID' and it can be obtained by:
         - Looking at the logs when running pa_dlna.
         - Running the pa_dlna upnp_cmd program and entering the
           'device [IDX]' command followed by the 'udn' command.

    2) Otherwise use the first matching encoder listed in the 'selection'
       option of the 'DEFAULT' section. The 'selection' option is a comma
       separated list of encoders. This option can be customized as all other
       options.

    Notes:
    The 'udns' and 'selection' options may be written as a multi-line in which
    case all the lines after the first line MUST START with a white space.

    The default value of 'selection' (before any customization) lists first
    the lossless encoders and then the lossy ones.
    See https://trac.ffmpeg.org/wiki/Encode/HighQualityAudio.
    """

    def __init__(self):
        endianess = sys.byteorder
        self._pulse_format = 's16le' if endianess == 'little' else 's16be'
        self.udns = ''
        self.rate = 44100
        self.channels = 2

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

    'format' may be set to 's16be' or 's16le'. Use the 's16be' format on a big
    endian DLNA device and 's16le' on a little endian one.

    To play and check the result obtained using a TestMediaRenderer with the
    '--renderers audio/L16;rate=44100;channels=2' command line argument, one may
    use the 'ffplay' tool from ffmpeg and, for example when the 'format' option
    is 's16le', run the command:

        $ ffplay -f s16le -ac 2 -ar 44100 output_file

    See also https://datatracker.ietf.org/doc/html/rfc2586.
    """

    def __init__(self):
        self._available = True
        self._mime_types = ['audio/l16']
        super().__init__()
        self.format = 's16le'

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
        if mime_type in self._mime_types:
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
