.. _pa-dlna:

pa-dlna
=======

Synopsis
--------

:program:`pa-dlna` [*options*]

Options
-------

.. option::  -h, --help

   Show this help message and exit.

.. option::  --version, -v

   Show program's version number and exit.

.. option:: --nics NICS, -n NICS

   NICS is a comma separated list of the names of network interface controllers
   where UPnP devices may be discovered, such as ``wlan0,enp5s0`` for
   example. All the interfaces are used when this option is an empty string or
   the option is missing (default: ``''``)

.. option::  --msearch-interval MSEARCH_INTERVAL, -m MSEARCH_INTERVAL

   Set the time interval in seconds between the sending of the MSEARCH datagrams
   used for device discovery (default: 60)

.. option::  --ttl TTL

   Set the IP packets time to live to TTL (default: 2).

.. option::  --port PORT

   Set the TCP port on which the HTTP server handles DLNA requests (default:
   8080).

.. option::  --dump-default, -d

   Write to stdout (and exit) the default built-in configuration.

.. option::  --dump-internal, -i

   Write to stdout (and exit) the configuration used internally by the program
   on startup after the pa-dlna.conf user configuration file has been parsed.

.. option::  --loglevel {debug,info,warning,error}, -l {debug,info,warning,error}

   Set the log level of the stderr logging console (default: info).

.. option::  --logfile PATH, -f PATH

   Add a file logging handler set at 'debug' log level whose path name is PATH.

.. option::  --nolog-upnp, -u

   Ignore UPnP log entries at 'debug' log level.

.. option::  --log-aio, -a

   Do not ignore asyncio log entries at 'debug' log level; the default is to
   ignore those verbose logs.

.. option::  --test-devices MIME-TYPES, -t MIME-TYPES

   MIME-TYPES is a comma separated list of distinct audio mime types. A
   DLNATestDevice is instantiated for each one of these mime types and
   registered as a virtual DLNA device. Mostly for testing.
