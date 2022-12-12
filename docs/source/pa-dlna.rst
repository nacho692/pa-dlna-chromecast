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

.. option::  --networks NETWORKS, -n NETWORKS

   NETWORKS is a comma separated list of local IPv4 interfaces or local IPv4
   addresses where UPnP devices may be discovered. An IPv4 interface is written
   using the "IP address/network prefix" slash notation (aka CIDR notation) as
   printed by the 'ip address' linux command. When this option is an empty
   string or the option is missing, all the interfaces are used, except the
   127.0.0.1/8 loopback interface.

.. option::  --port PORT

   Set the TCP port on which the HTTP server handles DLNA requests (default:
   8080).

.. option::  --ttl TTL

   Set the IP packets time to live to TTL (default: 2).

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

   MIME-TYPES is a comma separated list of different audio mime types. A
   DLNATestDevice is instantiated for each one of these mime types and
   registered as a plain DLNA device.
