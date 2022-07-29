import subprocess
import shutil

DEFAULT_CONFIG = (
                  'FFMpegMp3Encoder',
                  'FFMpegAacEncoder',
                  'FFMpegFlacEncoder',
                  )

class Encoder:
    """Configuration file for pa-dlna.

    This file is used to find an encoder matching one of the mime-types
    supported by a discovered DLNA device. The selection is made as follows:

    1) Use the first encoder whose 'udns' option holds the UDN (Unique Device
       Name) of the device, 'udns' is a comma separated list of UDNs.
       An UDN value has the format 'uuid:UUID' and it can be obtained by
       running the upnp_control program or looking at the logs when running
       pa_dlna with log level set at 'debug'.

    2) Otherwise use the first matching encoder listed in the 'selection'
       option of the 'DEFAULT' section. The 'selection' option is a comma
       separated list of encoders.

    Note:
    The 'udns' and 'selection' options may be written as a multi-line in which
    case all the lines after the first line MUST START with a white space.
    """

    def __init__(self):
        self.udns = ''

ROOT_ENCODER = Encoder

class FFMpegEncoder(Encoder):
    """See 'Guidelines for high quality lossy audio encoding'

    at https://trac.ffmpeg.org/wiki/Encode/HighQualityAudio
    """

    PGM = None
    ENCODERS = None

    def __init__(self, mime_types, codec):
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

    def add_args(self, cmd):
        return cmd

    def ffmpeg_cmd(self):
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

class FFMpegMp3Encoder(FFMpegEncoder):
    """Setting 'bitrate' to 0 causes VBR encoding to be chosen and 'qscale'
    to be used instead. See https://trac.ffmpeg.org/wiki/Encode/MP3.

    'bitrate' is expressed in kilobits.
    """

    def __init__(self):
        super().__init__(['audio/mpeg', 'audio/mp3'], 'mp3')
        self.bitrate = 256
        self.qscale = 2

    def add_args(self, cmd):
        if self.bitrate != 0:
            cmd.extend(['-b:a', f'{self.bitrate}k'])
        else:
            cmd.extend(['-qscale:a', f'{self.qscale}'])
        return cmd

class FFMpegFlacEncoder(FFMpegEncoder):

    def __init__(self):
        super().__init__(['audio/flac', 'audio/x-flac'], 'flac')


class FFMpegWavEncoder(FFMpegEncoder): pass
class FFMpegL16Encoder(FFMpegEncoder): pass
class FFMpegOggEncoder(FFMpegEncoder): pass
class FFMpegOpusEncoder(FFMpegEncoder): pass
