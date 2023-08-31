Usage
=====

:ref:`pa-dlna` usage
--------------------

The association between an application as a Pulseaudio source (music player,
firefox, etc...) and a DLNA device must be explicitly configured. See the
:ref:`source-sink` section below.

The :ref:`pa-dlna` section lists the command line options.

DLNA device discovery
"""""""""""""""""""""

UPnP discovery is triggered by NICs [#]_ state changes. That is, whenever a
configured NIC or the NIC of a configured IP address becomes up. Here are some
examples of events triggering UPnP discovery on an IP address after ``pa-dlna``
or ``upnp-cmd`` [#]_ has been started:

  - A wifi controller connects to a hotspot and acquires a new IP address
    through DHCP, possibly a different address from the previous one.
  - A static IP address has been configured on an ethernet card connected to an
    ethernet switch and the switch is turned on.

``pa-dlna`` registers a new sink with Pulseaudio upon the discovery of a DLNA
device and selects an encoder (see the :ref:`configuration` section for how the
encoder is selected).

The sink appears in the ``Output Devices`` tab of the ``pavucontrol`` graphical
tool and is listed by the ``pacmd`` and by the ``pactl`` Pulseaudio commands.

.. _source-sink:

Source-sink association
"""""""""""""""""""""""

Pulseaudio remembers the association between a source and a sink across
different sessions. A thorough description of this feature is given in
"PulseAudio under the hood" at `Automatic setup and routing`_.

Use ``pavucontrol``, ``pacmd`` or ``pactl`` [#]_ to establish this association
between a source and a DLNA device. Establishing this association is needed only
once.

  With ``pavucontrol``:
    In the ``Playback`` tab, use the drop-down list of the source to select the
    DLNA sink registered by ``pa-dlna``.

  With ``pacmd``:
    Get the list of sinks and find the index of the registered DLNA sink::

      $ pacmd list-sinks | grep -e 'name:' -e 'index'

    Get the list of sources and find the index of the source [#]_::

      $ pacmd list-sink-inputs | grep -e 'binary' -e 'index'

    Using both indexes create the association between the sink input and the
    DLNA sink registered by ``pa-dlna``::

      $ pacmd move-sink-input <sink-input index> <sink index>

When the DLNA device is not registered (``pa-dlna`` is not running or the DLNA
device is turned off) Pulseaudio temporarily uses the default sink as the sink
for this association. It is usually the host's sound card. See `Default/fallback
devices`_.

:ref:`upnp-cmd` usage
---------------------

An interactive command line tool for introspection and control of UPnP
devices.

The :ref:`upnp-cmd` section lists the command line options.

Some examples:

    - When the UPnP device [#]_ is a DLNA device [#]_, running the
      ``GetProtocolInfo`` command in the ``ConnectionManager`` service menu
      prints the list of mime types supported by the device.
    - Commands in the ``RenderingControl`` service allow to control the volume
      or mute the device.

**Note**: Upon ``upnp-cmd`` startup one must allow for the device discovery
process to complete before being able to select a device.

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
        [ConnectionManager] or ``friendlyName`` of the selected device when an
        embedded device has been selected.

One can select a DLNA device in the main menu and select a service or an
embedded device in the device menu.

UPnP Library
------------

UPnP devices are discovered by broadcasting MSEARCH SSDPs every 60 seconds (the
default) and by handling the NOTIFY SSDPs broadcasted by the devices.

The ``max-age`` directive in MSEARCH responses and NOTIFY broadcasts refreshes
the aging time of the device. The device is discarded of the list of registered
devices when this aging time expires.

UPnP eventing is not supported.

.. include:: common.txt

.. _Default/fallback devices:
        https://www.freedesktop.org/wiki/Software/PulseAudio/Documentation/User/DefaultDevice/
.. _Automatic setup and routing:
        https://gavv.net/articles/pulseaudio-under-the-hood/#automatic-setup-and-routing

.. rubric:: Footnotes

.. [#] Network Interface Controller.
.. [#] The list of the IP addresses where UPnP discovery is currently activated
       can be listed on ``upnp-cmd`` by printing the value of the
       ``ip_monitored`` variable in the main menu.
.. [#] ``pacmd`` is not supported by PipeWire, use ``pactl`` instead.
.. [#] A source is called a sink-input by Pulseaudio.
.. [#] An UPnP device implements the `UPnP Device Architecture`_ specification.
.. [#] A DLNA device is an UPnP device and implements the `MediaRenderer
       Device`_ specification and the `ConnectionManager`_, `AVTransport`_ and
       `RenderingControl`_ services.
