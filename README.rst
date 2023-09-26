`pa-dlna`_ forwards audio streams to DLNA devices.

A Python project based on `asyncio`_, that uses `ctypes`_ to interface with the
``libpulse`` library [#]_ and supports the PulseAudio and PipeWire [#]_ sound
servers.

`pa-dlna`_ is composed of the following components:

 * The ``pa-dlna`` program forwards PulseAudio streams to DLNA devices.
 * The ``upnp-cmd`` is an interactive command line tool for introspection and
   control of UPnP devices [#]_.
 * The UPnP Python package is used by both commands.
 * The pulselib Python package is a ctypes interface to the ``libpulse``
   library.

See the **pa-dlna** `documentation`_.

Installation
------------

Install ``pa-dlna`` with pip::

  $ python -m pip install pa-dlna

Requirements
------------

Python version 3.8 or more recent.

The built-in UPnP package  and therefore the ``upnp-cmd`` and ``pa-dlna``
commands depend on the `psutil`_ Python package. No other dependency is required
by `pa-dlna`_ when the DLNA devices support raw PCM L16 (:rfc:`2586`) [#]_.

Optionally, encoders compatible with the audio mime types supported by the
devices may be used. ``pa-dlna`` currently supports the `ffmpeg`_ (mp3, wav,
aiff, flac, opus, vorbis, aac), the `flac`_ and the `lame`_ (mp3) encoders. The
list of supported encoders, whether they are available on this host and their
options, is printed by the command that prints the default configuration::

  $ pa-dlna --dump-default

Optionally, one may install the ``pavucontrol`` package for easier management of
associations between sound sources and DLNA devices.

Configuration
-------------

A ``pa-dlna.conf`` user configuration file overriding the default configuration
may be used to:

 * Change the preferred encoders ordered list used to select an encoder.
 * Configure encoder options.
 * Set an encoder for a given device and configure the options for this device.
 * Configure the *sample_format*, *rate* and *channels* parameters of the
   ``parec`` program used to forward PulseAudio streams, for a specific device,
   for an encoder type or for all devices.

See the `configuration`_ section of the ``pa-dlna`` `documentation`_.

.. _pa-dlna: https://gitlab.com/xdegaye/pa-dlna
.. _asyncio: https://docs.python.org/3/library/asyncio.html
.. _ctypes: https://docs.python.org/3/library/ctypes.html
.. _pa-dlna issue 15: https://gitlab.com/xdegaye/pa-dlna/-/issues/15
.. _Wireplumber issue 511:
        https://gitlab.freedesktop.org/pipewire/wireplumber/-/issues/511
.. _documentation: https://pa-dlna.readthedocs.io/en/stable/
.. _psutil: https://pypi.org/project/psutil/
.. _ConnectionManager:3 Service:
        http://upnp.org/specs/av/UPnP-av-ConnectionManager-v3-Service.pdf
.. _ffmpeg: https://www.ffmpeg.org/ffmpeg.html
.. _flac: https://xiph.org/flac/
.. _lame: https://lame.sourceforge.io/
.. _configuration: https://pa-dlna.readthedocs.io/en/stable/configuration.html
.. _pipewire-pulse: https://docs.pipewire.org/page_man_pipewire_pulse_1.html

.. [#] The libpulse library is usually installed by the distributions as a
       dependency of PulseAudio or PipeWire when installed with the
       `pipewire-pulse`_ PulseAudio-compatible daemon.
.. [#] When using PipeWire with the Wireplumber session manager, ``pa-dlna``
       must be started before the audio streams that are routed to DLNA
       devices. Re-starting those audio  streams fixes the problem. See `pa-dlna
       issue 15`_ and `Wireplumber issue 511`_.
.. [#] The ``pa-dlna`` and ``upnp-cmd`` programs can be run simultaneously.
.. [#] DLNA devices must support the HTTP GET transfer protocol and must support
       HTTP 1.1 as specified by Annex A.1 of the `ConnectionManager:3 Service`_
       UPnP specification.
