Usage
=====

:ref:`pa-dlna` usage
--------------------

In this section:

    - A short description of :ref:`networking` relevant to ``pa-dlna``, the list
      of the UDP/TCP ports being used and what may be done when a firewall is in
      use.
    - Events triggering :ref:`discovery` and what happens then.
    - Configuration of a :ref:`source-sink` between an application as a
      Pulseaudio source (music player, firefox, etc...) and a DLNA device.

The :ref:`pa-dlna` section lists the pa-dlna command line options.

.. _networking:

DLNA Networking
"""""""""""""""

UPnP device discovery (and therefore DLNA device discovery) is implemented by two
protocols that run independently:

    1. To search for devices, an UPnP control point such as pa-dlna:

       - Send MSEARCH UDP multicast datagrams to ``239.255.255.250:1900``.
       - Listen to the source IP address and **source UDP port** that is used
         to send the MSEARCH request for the responses that are sent by the
         devices.
    2. To be notified of UPnP device advertisements, an UPnP control point
       listens on UDP port ``1900`` to receive NOTIFY UDP multicast datagrams
       broadcasted by the devices.

When pa-dlna is ready to forward a Pulseaudio stream to a DLNA device, it starts
an HTTP server, if not already running, that listens on TCP port 8080 (the
default) at the local IP address of the network that has been used to discover
the DLNA device. This HTTP server only accepts connection requests from the IP
addresses of DLNA devices that have been learnt by pa-dlna. The HTTP session is
used to forward the Pulseaudio stream.

Ports that must be enabled on a network interface by a firewall:

    - MSEARCH UDP port:
        This is the UDP port specified by the ``--msearch-port`` command line
        option of ``pa-dlna``. This option may be used to set the specific
        **source UDP port** [#]_ of MSEARCH UDP datagrams so that this port may
        be enabled by a firewall. Otherwise if this option is not used or set to
        0 the source port is chosen randomly by the operating system and it is
        necessary to configure the firewall to enable all UDP ports on the
        network interface.

    - NOTIFY UDP port:
        The port value is set by the UPnP specifications as ``1900``. When
        blocked by a firewall, UPnP device advertisements are not received but
        UPnP devices are still discovered with MSEARCH.

    - HTTP server's TCP port:
        This is the TCP port specified by the ``--port`` command line
        option of ``pa-dlna``. The default is port ``8080``.

.. _discovery:

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
tool and is listed by the ``pactl`` Pulseaudio commands.

.. _source-sink:

Source-sink association
"""""""""""""""""""""""

Pulseaudio remembers the association between a source and a sink across
different sessions. A thorough description of this feature is given in
"PulseAudio under the hood" at `Automatic setup and routing`_.

Use ``pavucontrol`` or ``pactl`` to establish this association between a source
and a DLNA device while the source is playing and the DLNA device has been
registered with Pulseaudio. Establishing this association is needed only once.

  With ``pavucontrol``:
    In the ``Playback`` tab, use the drop-down list of the source to select the
    DLNA sink registered by ``pa-dlna``.

  With ``pactl``:
    Get the list of sinks and find the index of the registered DLNA sink::

      $ pactl list sinks | grep -e 'Sink' -e 'Name'

    Get the list of sources and find the index of the source [#]_; the source
    must be playing::

      $ pactl list sink-inputs | grep -e 'Sink Input' -e 'binary'

    Using both indexes create the association between the sink input and the
    DLNA sink registered by ``pa-dlna``::

      $ pactl move-sink-input <sink-input index> <sink index>

When the DLNA device is not registered (``pa-dlna`` is not running or the DLNA
device is turned off) Pulseaudio temporarily uses the default sink as the sink
for this association. It is usually the host's sound card. See `Default/fallback
devices`_.

:ref:`upnp-cmd` usage
---------------------

An interactive command line tool for introspection and control of UPnP
devices.

The :ref:`upnp-cmd` section lists the upnp-cmd command line options.

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

.. [#] Prefer choosing a port in the range 49152–65535.
.. [#] Network Interface Controller.
.. [#] The list of the IP addresses learnt by pa-dlna through UPnP discovery may
       be listed with ``upnp-cmd`` by printing the value of the ``ip_monitored``
       variable in the main menu.
.. [#] A source is called a sink-input by Pulseaudio.
.. [#] An UPnP device implements the `UPnP Device Architecture`_ specification.
.. [#] A DLNA device is an UPnP device and implements the `MediaRenderer
       Device`_ specification and the `ConnectionManager`_, `AVTransport`_ and
       `RenderingControl`_ services.
