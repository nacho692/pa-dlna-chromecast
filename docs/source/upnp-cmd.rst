.. _upnp-cmd:

upnp-cmd
========

Synopsis
--------

:program:`upnp-cmd` [*options*]

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

.. option::  --logfile PATH, -f PATH

   Add a file logging handler set at 'debug' log level whose path name is PATH.

.. option::  --nolog-upnp, -u

   Ignore UPnP log entries at 'debug' log level.

.. option::  --log-aio, -a

   Do not ignore asyncio log entries at 'debug' log level; the default is to
   ignore those verbose logs.
