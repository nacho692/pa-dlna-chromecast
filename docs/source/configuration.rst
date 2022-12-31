Configuration
=============

Encoders Configuration
----------------------

The encoders configuration is based on the :ref:`default_config` that may be
customized by the user's ``pa-dlna.conf`` file. The :ref:`default_config` [#]_ is
printed on stdout with the ``--dump-default`` option of the ``pa-dlna``
program. This output is structured as an `INI file`_, more precisely as text
that could be parsed by the `configparser`_ Python module. The user's configuration
file is also an INI file and obeys the same rules as the default configuration
ones:

    * A section is either [DEFAULT], [EncoderName] or [EncoderName.UDN]. The
      options defined in the [DEFAULT] section apply to all the other sections
      and are overriden when also defined in the [EncoderName] or
      [EncoderName.UDN] sections. There is an exception with the ``selection``
      option that is only meaningful in a [DEFAULT] section and ignored in all
      the other sections.
    * The ``selection`` option is an ordered comma separated list of
      encoders. This list is used to select the first encoder matching one of
      the mime-types supported by a discovered DLNA device when there is no
      specific [EncoderName.UDN] configuration for the given device.
    * The options defined in the user's ``pa-dlna.conf`` file override the
      options of the default configuration.
    * The ``pa-dlna.conf`` file also allows the specification of the encoder for
      a given DLNA device with a section named [EncoderName.UDN]. In this case
      the selection of the encoder using the ``selection`` option is by-passed
      and EncoderName is the selected encoder. UDN is the udn of the device as
      printed by the logs or by the ``upnp-cmd`` program.
    * Section names and options are case sensitive.
    * Boolean values are resticted to ``yes`` or ``no``.

User Configuration
------------------

The full path name of the  user's ``pa-dlna.conf`` file is determined by
``pa-dlna`` as follows:

    * If the ``XDG_CONFIG_HOME`` environment variable is set, the path name is
      ``$XDG_CONFIG_HOME/pa-dlna/pa-dlna.conf``.
    * Otherwise the path name is ``$HOME/.config/pa-dlna/pa-dlna.conf``.

When ``pa-dlna.conf`` is not found, the program uses the default configuration,
otherwise as stated in the previous section, it uses the default configuration
with its options overriden by the user's configuration and with the added
[EncoderName.UDN] sections.

Here is an example of a ``pa-dlna.conf`` file::

    [DEFAULT]
    selection =
        Mp3Encoder,
        FFMpegMp3Encoder,
        FFMpegFlacEncoder,

    [FFMpegFlacEncoder]
    track_metadata = no

    [FFMpegMp3Encoder]
    bitrate = 320

    [FFMpegMp3Encoder.uuid:9ab0c000-f668-11de-9976-00a0de98381a]

In this example:

    * The DLNA device whose udn is ``uuid:9ab0c000-f668-11de-9976-00a0de98381a``
      uses the FFMpegMp3Encoder with the default bitrate.
    * The other devices may use the three encoders of the selection, the
      preferred one being the Mp3Encoder with the default bitrate.
    * The FFMpegMp3Encoder is only used if the Mp3Encoder (the lame encoder) is
      not available and in that case it runs with a bitrate of 320 Kbps.
    * The FFMpegFlacEncoder is used when a DLNA device does not support the
      'audio/mp3' and 'audio/mpeg' mime types and in that case its
      track_metadata option is not set.
    * If a DLNA device does not support the mp3 or the flac mime types, then it
      cannot be used even though the device would support one of the other mime
      types defined in the default configuration.

One can verify what is the actual configuration used by ``pa-dlna`` by running
the program with the ``--dump-internal`` command line option. A Python
dictionary is printed with keys being ``EncoderName`` or ``UDN`` and the values
a dictionary of their options. The ``EncoderName`` keys are ordered according to
the ``selection`` option.

Options
-------

The options common to all sections are:

  *args*
    The arguments of the encoder program's command line. The encoder's specific
    options, such as bitrate for example, are ignored when this option is
    overriden in the ``pa-dlna.conf`` file.

  *channels*
    The number of audio channels.

  *rate*
    The pulseaudio sample rate.

  *track_metadata*
    * When ``yes``, each track is streamed in its own HTTP session allowing the
      DLNA device to get the track meta data as described in the :ref:`meta
      data` section. This is the default.
    * When ``no``, there is only one HTTP session for all the tracks. Set this
      option to ``no`` when the logs show ERROR entries upon tracks changes.

.. _INI file: https://en.wikipedia.org/wiki/INI_file
.. _configparser:
        https://docs.python.org/3/library/configparser.html#supported-ini-file-structure

.. rubric:: Footnotes

.. [#] This text is generated from Python classes in the ``pa-dlna`` source
       code.
