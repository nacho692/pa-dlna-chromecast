import subprocess
import shutil

DEFAULT_CONFIG = (
    'FFMpegFlacEncoder',
    'FFMpegL16Encoder',
    'FFMpegOpusEncoder',
    'FFMpegVorbisEncoder',
    'FFMpegMp3Encoder',
    'FFMpegAacEncoder',
)

def select_encoder(encoders, protocols, udn):
    """Select the encoder.

    Return the selected encoder and the mime type.
    """

    def available_encoders(encoders):
        return (instance for instance in encoders.values()
                if instance.available)

    for encoder in available_encoders(encoders):
        if udn in (u.strip() for u in encoder.udns.split(',')):
            return encoder, encoder.mime_types[0]

    for encoder in available_encoders(encoders):
        for protocol in protocols:
            if protocol.lower() in encoder.mime_types:
                return encoder, protocol

class Encoder:
    """INI configuration file for pa-dlna.

    This file is used to find an encoder matching one of the mime-types
    supported by a discovered DLNA device. The selection is made as follows:

    1) Use the first encoder whose 'udns' option holds the UDN (Unique Device
       Name) of the device, 'udns' is a comma separated list of UDNs.
       An UDN value has the format 'uuid:UUID' and it can be obtained by:
         - looking at the logs when running pa_dlna with log level set at
           'debug'
         - or running the pa_dlna upnp_cmd program and entering the
           'device [IDX]' command followed by the 'udn' command.

    2) Otherwise use the first matching encoder listed in the 'selection'
       option of the 'DEFAULT' section. The 'selection' option is a comma
       separated list of encoders. This option can be customized as all other
       options.

    Notes:
    The 'udns' and 'selection' options may be written as a multi-line in which
    case all the lines after the first line MUST START with a white space.

    The default value of 'selection' before any customization lists first the
    ffmpeg lossless encoders FLAC, L16 and then the lossy ones according to
    https://trac.ffmpeg.org/wiki/Encode/HighQualityAudio.
    """

    def __init__(self):
        self.udns = ''

    @property
    def available(self):
        return self._available

    @property
    def mime_types(self):
        return self._mime_types

    @property
    def command(self):
        return self._command()

ROOT_ENCODER = Encoder

class FFMpegEncoder(Encoder):

    PGM = None
    ENCODERS = None

    def __init__(self, mime_types, codec, encoder=None):
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
        self.args = (f'-loglevel fatal -hide_banner -nostats'
                     f' -ac 2 -ar 44100 -f s16le -i -'
                     f' -f {codec}')
        if encoder is not None:
            self.args += f' -c:a {encoder}'

    def add_args(self, cmd):
        return cmd

    def _command(self):
        cmd = [self._pgm]
        cmd.extend(self.args.split())
        cmd = self.add_args(cmd)
        cmd.append('pipe:1')
        return cmd


class FFMpegAacEncoder(FFMpegEncoder):
    """See https://trac.ffmpeg.org/wiki/Encode/AAC.

    'bitrate' is expressed in kilobits.
    """

    def __init__(self):
        super().__init__(['audio/aac', 'audio/x-aac'], 'aac')
        self.bitrate = 192

    def add_args(self, cmd):
        cmd.extend(['-b:a', f'{self.bitrate}k'])
        return cmd

class FFMpegFlacEncoder(FFMpegEncoder):
    """See https://ffmpeg.org/ffmpeg-all.html#flac-2."""

    def __init__(self):
        super().__init__(['audio/flac', 'audio/x-flac'], 'flac')

class FFMpegL16Encoder(FFMpegEncoder):
    """See https://datatracker.ietf.org/doc/html/rfc2586."""

    def __init__(self):
        super().__init__(['audio/l16'], 's16be')
        self.rate = 44100
        self.channels = 2

    def is_mime_type(self, protocol):
        # For example 'audio/L16;rate=44100;channels=2'.
        protocol = [p.strip() for p in protocol.lower().split(';')]
        if protocol[0] != self._mime_types[0]:
            return False

        rate_channels = [0, 0]
        for param in protocol[1:]:
            for (n, prefix) in enumerate(['rate=', 'channels=']):
                if param.startswith(prefix):
                    try:
                        rate_channels[n] = int(param[len(prefix):])
                    except ValueError:
                        return False
                    break
        if rate_channels[0] != 0:
            self.rate = rate_channels[0]
            if rate_channels[1] != 0:
                self.channels = rate_channels[1]
            return True

    def add_args(self, cmd):
        cmd.extend(['-ar', str(self.rate)])
        cmd.extend(['-ac', str(self.channels)])
        return cmd

class FFMpegMp3Encoder(FFMpegEncoder):
    """Setting 'bitrate' to 0 causes VBR encoding to be chosen and 'qscale'
    to be used instead. See https://trac.ffmpeg.org/wiki/Encode/MP3.

    'bitrate' is expressed in kilobits.
    """

    def __init__(self):
        super().__init__(['audio/mp3', 'audio/mpeg'], 'mp3', 'libmp3lame')
        self.bitrate = 256
        self.qscale = 2

    def add_args(self, cmd):
        if self.bitrate != 0:
            cmd.extend(['-b:a', f'{self.bitrate}k'])
        else:
            cmd.extend(['-qscale:a', str(self.qscale)])
        return cmd

class FFMpegOpusEncoder(FFMpegEncoder):
    """See https://wiki.xiph.org/Opus_Recommended_Settings."""

    def __init__(self):
        super().__init__(['audio/opus', 'audio/x-opus'], 'opus', 'libopus')
        self.bitrate = 128

    def add_args(self, cmd):
        cmd.extend(['-b:a', f'{self.bitrate}k'])
        return cmd

class FFMpegVorbisEncoder(FFMpegEncoder):
    """Setting 'bitrate' to 0 causes VBR encoding to be chosen and 'qscale'
    to be used instead. See https://ffmpeg.org/ffmpeg-all.html#libvorbis.

    'bitrate' is expressed in kilobits.
    """

    def __init__(self):
        super().__init__(['audio/vorbis', 'audio/x-vorbis'], 'vorbis',
                         'libvorbis')
        self.bitrate = 256
        self.qscale = 3.0

    def add_args(self, cmd):
        if self.bitrate != 0:
            cmd.extend(['-b:a', f'{self.bitrate}k'])
        else:
            cmd.extend(['-qscale:a', str(self.qscale)])
        return cmd
