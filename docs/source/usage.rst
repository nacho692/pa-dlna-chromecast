Usage
=====

:ref:`pa-dlna`
--------------

Basic operation
"""""""""""""""

``pa-dlna`` discovers DLNA devices and selects the encoder to be used for a
given device based on the availability of the encoders on the host, the list of
the device preferred mime types and the list of ``pa-dlna`` preferred encoders
from the built-in configuration, overriden by the user configuration file if
any.

Once the encoder is selected, a new sink is registered with pulseaudio and an
HTTP server is started.

Then when there is an association between a pulseaudio source and this sink (see
below), ``pa-dlna`` sends an URL to the device so that the device may fetch the
corresponding audio stream by issuing an HTTP GET for this URL.

See the :ref:`pa-dlna` man page.

Source-sink association
"""""""""""""""""""""""

``pa-dlna`` registers a new sink with pulseaudio upon the discovery of a DLNA
device. The sink appears in the ``Output Devices`` tab of the ``pavucontrol``
graphical tool and is listed by ``pacmd`` [#]_ with the command::

  $ pacmd list-sinks | grep -e 'name:' -e 'index'

Pulseaudio remembers the association between a source and a sink across
different sessions. A thorough description of this feature is given in
"PulseAudio under the hood" at `Automatic setup and routing`_. Use
``pavucontrol`` or ``pacmd`` to establish this association.

  With ``pavucontrol``:
    In the ``Playback`` tab, use the drop-down list of the source to select the
    DLNA sink registered by ``pa-dlna``.

  With ``pacmd``:
    Get the list of sources [#]_::

      $ pacmd list-sink-inputs | grep -e 'binary' -e 'index'

    Using the sink-input index from the previous command and the sink index from
    the first ``pacmd`` above, move the sink input to the DLNA  sink registered
    by ``pa-dlna``::

      $ pacmd move-sink-input <sink-input index> <sink index>

Establishing this association is needed only once. When ``pa-dlna`` is not
running or the DLNA device is turned off, pulseaudio temporarily uses the
default sink as the sink for this association, it is usually the host's sound
card. See `Default/fallback devices`_.

Testing encoder options
"""""""""""""""""""""""

The value of the ``--test-devices`` command line option is a comma separated
list of distinct audio mime types. A DLNATestDevice is instantiated for each one
of these mime types and registered as a virtual DLNA device.

Use this feature to debug the failure of a DLNA device to handle an encoder with
specific options. The URL of the DLNATestDevice is printed on the logs when it
is registered. Use a program that supports HTTP 1.1, such as curl for example,
to collect the audio stream after the pulseaudio sink of the DLNATestDevice has
been associated with a source (see above).

DLNATestDevice URLs are built using the sha1 of the audio mime type and
therefore are consistent across ``pa-dlna`` sessions.

:ref:`upnp-cmd`
---------------

An interactive command line tool for introspection and control of UPnP
devices. For example, when the UPnP device [#]_ is a DLNA device [#]_, running
the ``GetProtocolInfo`` command in the ``ConnectionManager`` service with
``upnp-cmd`` allows to get the ordered list of mime types supported by the
device, and commands in the ``RenderingControl`` service allow to control the
volume or mute the device.

See the :ref:`upnp-cmd` man page.

**Note**: One must allow for the devices discovery process to complete before
being able to select a device after command startup.

Commands usage:

    * Command completion and command arguments completion is enabled with the
      ``<Tab>`` key.
    * Help on the current menu is printed by typing ``?`` or ``help``.
    * Help on one of the commands is printed by typing ``help <command name>``
      or ``? <command name>``.
    * Use the arrow keys for command line history.
    * When the UPnP device is a DLNA device and one is prompted for
      ``InstanceID`` by some commands, use one of the ``ConnectionIDs`` printed
      by ``GetCurrentConnectionIDs`` in the ``ConnectionManager`` service. This
      is usually ``0`` as most DLNA devices do not support
      ``PrepareForConnection`` and therefore support only one connection.
    * To return to the previous menu, type ``previous``.
    * To exit the command type ``quit``, ``EOF``, ``<Ctl-d>`` or ``<Ctl-c>``.

The menu hierarchy is as follows:

    1. Main menu prompt:
        [Control Point]

    2. Next submenu prompt:
        ``friendlyName`` of the selected device, for example [Yamaha RN402D].

    3. Next submenu prompt:
        Either the service name when a service has been selected as for example
        [ConnectionManager] or back to step 2 when an embedded device has been
        selected.

UPnP Library
------------

UPnP devices are discovered by broadcasting MSEARCH SSDPs every 60 seconds (the
default) and by handling the NOTIFY SSDPs broadcasted by the devices.

The ``max-age`` directive in MSEARCH responses and NOTIFY broadcasts refreshes
the aging time of the device. The device is discarded of the list of registered
devices when this aging time expires.

Control of the UPnP device is done with the ``soap_action()`` method of an
``UPnPService`` instance.

Eventing is not supported.

.. include:: common.txt

.. _Default/fallback devices:
        https://www.freedesktop.org/wiki/Software/PulseAudio/Documentation/User/DefaultDevice/
.. _Automatic setup and routing:
        https://gavv.net/articles/pulseaudio-under-the-hood/#automatic-setup-and-routing

.. rubric:: Footnotes

.. [#] ``pavucontrol`` and ``pacmd`` are  part of pulseaudio and installed with
       pulseaudio.
.. [#] A source is called a sink-input by pulseaudio.
.. [#] An UPnP device implements the `UPnP Device Architecture`_ specification.
.. [#] A DLNA device is an UPnP device and implements the `MediaRenderer
       Device`_ specification and the `ConnectionManager`_, `AVTransport`_ and
       `RenderingControl`_ services.
