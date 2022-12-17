A Python project based on asyncio composed of three components:

 * The ``pa-dlna`` program forwarding PulseAudio streams to DLNA devices.
 * The ``upnp-cmd`` interactive command line tool for introspection and control
   of UPnP devices.
 * A standalone UPnP library used by both commands.

See the ``pa-dlna`` `documentation`_.

Installation::

  $ pip install pa-dlna

Requirements
------------

Python version 3.8 or more recent.

The ``pa-dlna`` command uses the Python ``pulsectl`` and ``pulsectl-asyncio``
packages. They are automatically installed with pa-dlna when installing with
pip. Optionally this command uses the ``ip`` command from the `iproute2`_
package when the ``--networks`` command line option is not set. If the
`iproute2`_ package is not installed then one must specify the network
interfaces using the ``--networks`` option.

The ``pa-dlna`` command does not require any other dependency when the DLNA
devices support raw PCM L16 (:rfc:`2586`). If not, then encoders compatible with
the audio mime types supported by the devices are required. ``pa-dlna``
currently supports `ffmpeg`_ (mp3, wav, aiff, flac, opus, vorbis, aac), the
`flac`_ and the `lame`_ (mp3) encoders. The list of supported encoders, whether
they are available on this host and their options, is printed by the command::

  $ pa-dlna --dump-default

whose output is the :ref:`default_config`.

DLNA devices must support HTTP streaming and support HTTP 1.1 as specified by
Annex A.1 of the `ConnectionManager:3 Service`_ UPnP specification and
especially chunked transfer encoding.

The UPnP library does not have any external dependency.

The ``upnp-cmd`` command depends on the ``ip`` command when the ``--networks``
command line option is not set.

Configuration
-------------

A ``pa-dlna.conf`` user configuration file may be used to:

 * Change the preferred encoders ordered list used to select an encoder.
 * Customize encoder options.
 * Set an encoder for a given device and customize its options for this device.

.. _documentation: https://pa-dlna.readthedocs.io/en/latest/
.. _iproute2: https://en.wikipedia.org/wiki/Iproute2
.. _ConnectionManager:3 Service:
        http://upnp.org/specs/av/UPnP-av-ConnectionManager-v3-Service.pdf
.. _ffmpeg: https://www.ffmpeg.org/ffmpeg.html
.. _flac: https://xiph.org/flac/
.. _lame: https://lame.sourceforge.io/
