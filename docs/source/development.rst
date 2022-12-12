Development
===========

.. _design:

Design
------

**Meta data**

When ``pa-dlna`` receives a ``change`` event from pulseaudio and this event is
related to a change to the meta data as for example when a new track starts with
a new song, the following sequence of events occurs:

 * ``pa-dlna``:

   + Writes the last chunk to the HTTP socket (see `Chunked Transfer Coding`_)
     and sends a ``SetNextAVTransportURI`` soap action with the new meta data.
   + Upon receiving the HTTP GET request from the device, instantiates a new
     Track and starts its task to run the pulseaudio stream.

 * The DLNA device:

   + Gets the ``SetNextAVTransportURI`` with the new meta data and sends a GET
     request to start a new HTTP session for the next track while still playing
     the current track from its read buffer.
   + Still playing the current track, pre-loads the read buffer of the new HTTP
     session.
   + Upon receiving the last chunk for the current track, starts playing the
     next track.

This way, the last part of the current track is not truncated by the amount of
latency introduced by the device's read buffer and the delay introduced by
filling the read buffer of the next track is minimized.

**Renderer instantiation**

For a new Renderer to be instantiated one needs to know which one of the network
addresses set by the ``--networks`` command line option will be advertised to
the DLNA device in the ``SetAVTransportURI`` soap action:

  * If this is triggered by a unicast response to an UPnP msearch SSDP, then
    this is the destination address of the SSDP response and the Renderer can be
    instantiated.

  * If this is triggered by a notify SSDP, broadcasted by the device, then we
    can only instantiate the Renderer if the source address of this packet
    belongs to one of the network interfaces set by the ``--networks``
    option. Hence the reason why a network interface entry is preferred in this
    option.

**Asyncio tasks**

Task names in **bold** characters indicate that there is one such task for each
DLNA device, when in *italics* that there may be one such task for each DLNA
device.

  UPnPControlPoint tasks:

    ================      ======================================================
    ssdp notify           Monitor reception of notify SSDPs.
    ssdp msearch          Send msearch SSDPs at regular intervals.
    **root device**       Implement control of the aging of an UPnP root device.
    ================      ======================================================

  AVControlPoint tasks:

    ================      ======================================================
    main                  Instantiate the UPnPControlPoint that starts the UPnP
                          tasks -
                          Create the pulse task, the http_server task, the
                          renderer tasks and the shutdown task -
                          Handle UPnP notifications.

    pulse                 Monitor pulseaudio sink-input events.
    http_server           Serve DLNA HTTP requests and start the
                          client_connected tasks.
    **renderers**         Act upon pulseaudio events and run UPnP soap actions.
    abort                 Abort the pa-dlna program.
    shutdown              Wait on event pushed by the signal handlers.
    ================      ======================================================

  HTTPServer tasks:

    ==================    ======================================================
    *client_connected*    HTTPServer callback wrapped by asyncio in a task -
                          Start the tasks that forward the audio stream
                          from a pulseaudio null-sink monitor to the HTTP
                          socket via 'parec | encoder program | HTTP socket'.
    ==================    ======================================================

  StreamSession tasks:

    ====================    ====================================================
    *parec process*         Start the parec process and wait for its exit.
    *parec log_stderr*      Log the parec process stderr.
    *encoder process*       Start the encoder process and wait for its exit.
    *encoder log_stderr*    Log the encoder process stderr.
    *track*                 Write the audio stream to the HTTP socket.
    ====================    ====================================================

  Track tasks:

    ==============        ======================================================
    *shutdown*            Write the last chunk and close the HTTP socket.
    ==============        ======================================================

Releasing
---------

**Build the documentation**

Generate ``default-config.rst``, build html documentation and man pages::

  $ python -m tools.gendoc_default_config
  $ make -C docs clean html man

.. _Chunked Transfer Coding: https://www.rfc-editor.org/rfc/rfc2616#section-3.6.1
.. _UPnP AV Architecture:
        http://upnp.org/specs/av/UPnP-av-AVArchitecture-v2.pdf
