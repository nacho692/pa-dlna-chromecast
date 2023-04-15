.. _upnp-cmd:

upnp-cmd
========

Synopsis
--------

:program:`upnp-cmd` [*options*]

UPnP discovery is run on all the networks (except the loopbak interface ``lo``)
when the ``--ip-addresses`` and ``--nics`` command line arguments are not used
or empty. Otherwise both arguments may be used indifferently or even jointly.

Options
-------

.. option::  -h, --help

   Show this help message and exit.

.. option::  --version, -v

   Show program's version number and exit.

.. option:: --ip-addresses IP_ADDRESSES, -a IP_ADDRESSES

   IP_ADDRESSES is a comma separated list of the IPv4 addresses of the networks
   where UPnP devices may be discovered (default: ``''``).

.. option:: --nics NICS, -n NICS

   NICS is a comma separated list of the names of network interface controllers
   where UPnP devices may be discovered, such as ``wlan0,enp5s0`` for
   example (default: ``''``).

.. option::  --msearch-interval MSEARCH_INTERVAL, -m MSEARCH_INTERVAL

   Set the time interval in seconds between the sending of the MSEARCH datagrams
   used for device discovery (default: 60)

.. option::  --ttl TTL

   Set the IP packets time to live to TTL (default: 2).

.. option::  --logfile PATH, -f PATH

   Add a file logging handler set at ``debug`` log level whose path name is PATH.

.. option::  --nolog-upnp, -u

   Ignore UPnP log entries at ``debug`` log level.

.. option::  --log-aio, -y

   Do not ignore asyncio log entries at ``debug`` log level; the default is to
   ignore those verbose logs.
