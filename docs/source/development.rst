Development
===========

.. _design:

Design
------

.. _meta data:

Meta Data
"""""""""

When ``pa-dlna`` receives a ``change`` event from pulseaudio and this event is
related to a change to the meta data as for example when a new track starts with
a new song, the following sequence of events occurs:

 * ``pa-dlna``:

   + Writes the last chunk to the HTTP socket (see `Chunked Transfer Coding`_)
     and sends a ``SetNextAVTransportURI`` SOAP action with the new meta data.
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

This feature is enabled on a per encoder or per device basis with the
``track_metadata`` option. It is enabled by default.

Asyncio Tasks
"""""""""""""

Task names in **bold** characters indicate that there is one such task for each
DLNA device, when in *italics* that there may be such tasks for each DLNA
device.

  UPnPControlPoint tasks:

    ================      ======================================================
    ssdp notify           Monitor reception of NOTIFY SSDPs.
    ssdp msearch          Send MSEARCH SSDPs at regular intervals.
    **root devices**      Implement control of the aging of an UPnP root device.
    ================      ======================================================

  AVControlPoint tasks:

    ================      ======================================================
    main                  Instantiate the UPnPControlPoint that starts the UPnP
                          tasks.
                          |br| Create the pulse task, the http_server task, the
                          renderer tasks.
                          |br| Create the shutdown task.
                          |br| Handle UPnP notifications.

    pulse                 Monitor pulseaudio sink-input events.
    *http_server*         Serve DLNA HTTP requests, one task per IP address.
                          |br| Start the client_connected tasks.
    **renderers**         Act upon pulseaudio events.
                          |br| Run UPnP SOAP actions.
    abort                 Abort the pa-dlna program.
    shutdown              Wait on event pushed by the signal handlers.
    ================      ======================================================

  HTTPServer tasks:

    ==================    ======================================================
    *client_connected*    HTTPServer callback wrapped by asyncio in a task.
                          |br| Start the StreamSession tasks:
                          |br| ``parec | encoder program | HTTP socket``.
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

DLNA Device Registration
""""""""""""""""""""""""

For a new DLNA device to be registered, ``pa-dlna`` must establish the network
address to be used in the URL that must be  advertised to the DLNA device in the
``SetAVTransportURI`` SOAP action, so that the DLNA device may initiate the HTTP
session and start the streaming. This depends on which event triggered this
registration:

  Reception of the  unicast response to an UPnP MSEARCH SSDP.
    The destination address of the SSDP response is the address that is being
    looked for.

    MSEARCH SSDP are sent by ``pa-dlna`` every 60 seconds (default).

  Reception of an UPnP NOTIFY SSDP, broadcasted by the device [#]_.
    The DLNA device can be registered only if the source address of this packet
    belongs to one of the subnets of the network interfaces. That is, the DLNA
    device and the host belong to the same subnet on this interface and the
    local IP address on this subnet is the address that is being looked for.

    The `UPnP Device Architecture`_ specification does not specify the
    periodicity of NOTIFY SSDPs sent by DLNA devices.

Development process
-------------------

Requirements
""""""""""""

Development:
    * `flit`_ to publish pa-dlna to PyPi. Use the following command to install
      pa-dlna locally and have the changes in the source code reflected by the
      ``pa-dlna`` command::

        $ flit install --symlink [--python path/to/python_version]

Documentation:
    * `Sphinx`_ [#]_.
    * `Read the Docs theme`_.

Documentation
"""""""""""""

To build locally the documentation, generate the ``default-config.rst`` file,
build the html documentation and the man pages::

  $ python -m tools.gendoc_default_config
  $ make -C docs clean html man

Releasing
"""""""""

* Run the test suite [#]_::

    $ python -m unittest --verbose --catch --failfast

* Update ``__version__`` in pa_dlna/__init__.py.
* Update docs/source/history.rst
* Build locally the documentation.
* Commit the changes::

    $ git commit -m 'Version 0.n'
    $ git push

* Publish the new version to PyPi::

    $ flit publish

* Tag the release and push::

    $ git tag -a v0.n -m 'Version 0.n'
    $ git push --tags

.. include:: common.txt

.. _Chunked Transfer Coding:
    https://www.rfc-editor.org/rfc/rfc2616#section-3.6.1
.. _Read the Docs theme:
    https://docs.readthedocs.io/en/stable/faq.html#i-want-to-use-the-read-the-docs-theme-locally
.. _Sphinx: https://www.sphinx-doc.org/
.. _flit: https://pypi.org/project/flit/
.. _unittest command line options:
    https://docs.python.org/3/library/unittest.html#command-line-options

.. rubric:: Footnotes

.. [#] All sockets bound to the notify multicast address receive the datagram
       sent by a DLNA device, even though it has been received by only one
       interface at the physical layer.
.. [#] Required versions at ``docs/requirements.txt``.
.. [#] See `unittest command line options`_.
