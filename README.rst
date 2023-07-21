The **pa-dlna** Python project forwards pulseaudio streams to DLNA devices. It
is based on asyncio and uses ctypes to interface with the pulseaudio library.
It is composed of the following components:

 * The ``pa-dlna`` program forwards PulseAudio streams to DLNA devices.
 * The ``upnp-cmd``, an interactive command line tool for introspection and
   control of UPnP devices [#]_.
 * The UPnP library used by both commands.
 * The pulselib library, a ctypes interface to the pulseaudio library.

See the **pa-dlna** `documentation`_.

Installation::

  $ python -m pip install pa-dlna

Requirements
------------

Python version 3.8 or more recent.

DLNA devices must support the HTTP GET transfer protocol and must support HTTP
1.1 as specified by Annex A.1 of the `ConnectionManager:3 Service`_ UPnP
specification.

Dependencies
------------

The built-in UPnP library  and therefore the ``upnp-cmd`` and ``pa-dlna``
commands depend on the ``psutil`` Python package.

The ``pa-dlna`` command does not require any other dependency when the DLNA
devices support raw PCM L16 (:rfc:`2586`).

If not, then encoders compatible with the audio mime types supported by the
devices are required. ``pa-dlna`` currently supports `ffmpeg`_ (mp3, wav, aiff,
flac, opus, vorbis, aac), the `flac`_ and the `lame`_ (mp3) encoders. The list
of supported encoders, whether they are available on this host and their
options, is printed by the command::

  $ pa-dlna --dump-default

Configuration
-------------

A ``pa-dlna.conf`` user configuration file is used to:

 * Change the preferred encoders ordered list used to select an encoder.
 * Configure encoder options.
 * Set an encoder for a given device and configure its options for this device.
 * Configure for a device, for an encoder type or for all devices the
   *sample_format*, *rate* and *channels* parameters of the ``parec`` program
   used to forward pulseaudio streams.

.. _documentation: https://pa-dlna.readthedocs.io/en/stable/
.. _iproute2: https://en.wikipedia.org/wiki/Iproute2
.. _ConnectionManager:3 Service:
        http://upnp.org/specs/av/UPnP-av-ConnectionManager-v3-Service.pdf
.. _ffmpeg: https://www.ffmpeg.org/ffmpeg.html
.. _flac: https://xiph.org/flac/
.. _lame: https://lame.sourceforge.io/

.. [#] The ``pa-dlna`` and ``upnp-cmd`` programs can be run simultaneously.
