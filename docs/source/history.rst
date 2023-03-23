Release history
===============

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
  - Test coverage of the UPnP library is 94%.
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
