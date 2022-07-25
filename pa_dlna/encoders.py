# Defines the order in which the encoders are looked up for a match with the
# capabilities of the DLNA device.
DEFAULT_CONFIG = ('FFmpegMp3Encoder', 'FFmpegFlacEncoder',)

class Encoder:
    """This is Encoder.

    More text.
    """

ROOT_ENCODER = Encoder

class Mp3Encoder(Encoder): pass
class FlacEncoder(Encoder): pass
class WavEncoder(Encoder): pass
class L16Encoder(Encoder): pass
class AacEncoder(Encoder): pass
class OggEncoder(Encoder): pass
class OpusEncoder(Encoder): pass

class FFmpegMp3Encoder(Mp3Encoder):
    """This is B

    type: mp3
    """
    def __init__(self):
        self.a = 0
        self.b = 11
        self.ZZZ = 123.0

class FFmpegFlacEncoder(FlacEncoder):
    def __init__(self):
        self.a = False
        self.b = 22
