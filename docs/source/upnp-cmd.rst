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

.. option:: --nics NICS, -n NICS

   NICS is a comma separated list of the names of network interface controllers
   where UPnP devices may be discovered, such as ``wlan0,enp5s0`` for
   example. All the interfaces are used when this option is an empty string or
   the option is missing (default: ``''``)

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
