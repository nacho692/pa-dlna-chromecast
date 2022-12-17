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

This feature is enabled on a per encoder or per device basis with the
``track_metadata`` option. It is enabled by default.

Asyncio Tasks
"""""""""""""

Task names in **bold** characters indicate that there is one such task for each
DLNA device, when in *italics* that there may be one such task for each DLNA
device.

  UPnPControlPoint tasks:

    ================      ======================================================
    ssdp notify           Monitor reception of NOTIFY SSDPs.
    ssdp msearch          Send MSEARCH SSDPs at regular intervals.
    **root device**       Implement control of the aging of an UPnP root device.
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
    http_server           Serve DLNA HTTP requests.
                          |br| Start the client_connected tasks.
    **renderers**         Act upon pulseaudio events.
                          |br| Run UPnP soap actions.
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

Development
-----------

Requirements
""""""""""""

Development:
    * `flit`_ to publish pa-dlna to PyPi. Use the following command to install
      pa-dlna locally and have the changes in the source code reflected by the
      ``pa-dlna`` command::

        $ flit install --symlink [--python path/to/python_version]

Documentation:
    * `Sphinx`_.
    * `Read the Docs theme`_.

Documentation
"""""""""""""

To build locally the documentation, generate the ``default-config.rst`` file,
build the html documentation and the man pages::

  $ python -m tools.gendoc_default_config
  $ make -C docs clean html man

Releasing
"""""""""

* Update ``__version__`` in pa_dlna/__init__.py.
* Build locally the documentation.
* Commit the changes.
* Create a git tag for the new version.
* Publish the new version to PyPi.

.. include:: common.txt

.. _Chunked Transfer Coding:
    https://www.rfc-editor.org/rfc/rfc2616#section-3.6.1
.. _Read the Docs theme:
    https://docs.readthedocs.io/en/stable/faq.html#i-want-to-use-the-read-the-docs-theme-locally
.. _Sphinx: https://www.sphinx-doc.org/
.. _flit: https://pypi.org/project/flit/
