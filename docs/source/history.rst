Release history
===============

Version 0.6
  - Spread out UPnP SOAP actions that start/stop a stream (issue #16).
  - Fix the ``args`` option in the [EncoderName.UDN] section of the user
    configuration is always None.
  - Log a warning when the sink-input enters the ``suspended`` state.
  - Fix assertion error upon ``exit`` pulseaudio event (issue #14).
  - Support PipeWire. No change is needed to support PipeWire. The test suite
    runs successfully on PipeWire.
  - Fix no sound when pa-dlna is started while the track is already playing
    (issue #13).
  - Use the built-in pulselib package that uses ctypes to interface with the
    libpulse library and remove the dependency to ``pulsectl_asyncio``.
  - Wait for the http server to be ready before starting the renderer task. This
    also fixes the test_None_nullsink and test_no_path_in_request tests on
    GitLab CI/CD (issue #12).
  - Support Python 3.11.

Version 0.5
  - Log a warning upon an empty body in the HTTP response from a DLNA device
    (issue #11).
  - UPnP discovery is triggered by NICs [#]_ state changes (issue #10).
  - Add the ``--ip-addresses``, ``-a`` command line argument (issue #9).
  - Fix changing the ``args`` encoder option is ignored (issue #8).

Version 0.4
  - ``sample_format`` is a new encoder configuration option (issue #3).
  - The encoders sample format is ``s16le`` except for the ``audio/l16``
    encoder (issue #7).
  - The encoder command line is now updated with ``pa-dlna.conf`` user
    configuration (issue #6).
  - Fix the parec command line length keeps increasing at each new track when
    the encoder is set to track metadata (issue #5).
  - Fix failing to start a new stream session while the device is still playing
    when the encoder is set to not track metadata (issue #4).
  - Fix ``pa-dlna`` hangs when one types <Control-S> in the terminal where the
    program has been started (issue #2).

Version 0.3
  - The test coverage of ``pa-dlna`` is 95%.
  - UPnPControlPoint supports now the context manager protocol, not the
    asynchronous one.
  - UPnPControlPoint.get_notification() returns now QUEUE_CLOSED upon closing.
  - Fix some fatal errors on startup that were silent.
    Here are the  missing error messages that are now printed when one of those
    fatal errors occurs:

    + Error: No encoder is available.
    + Error: The pulseaudio 'parec' program cannot be found.
  - Fix curl: (18) transfer closed with outstanding read data remaining.
  - Fix a race condition upon the reception of an SSDP msearch response that
    occurs just after the reception of an SSDP notification and while the
    instantiation of the root device is not yet complete.
  - Failure to set SSDP multicast membership is reported only once.

Version 0.2
  - Test coverage of the UPnP package is 94%.
  - Fix unknown UPnPXMLFatalError exception.
  - The ``description`` commands of ``upnp-cmd`` don't prefix tags with a
    namespace.
  - Fix the ``description`` commands of ``upnp-cmd`` when run with Python 3.8.
  - Fix IndexError exception raised upon OSError in
    network.Notify.manage_membership().
  - Fix removing multicast membership when the socket is closed.
  - Don't print a stack traceback upon error parsing the configuration file.
  - Abort on error setting the file logging handler with ``--logfile PATH``.

Version 0.1
  - Publish the project on PyPi.

.. rubric:: Footnotes

.. [#] Network Interface Controller.
