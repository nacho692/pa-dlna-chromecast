systemd
=======

Usage
-----

The `python-systemd`_ package is required to run the pa-dlna systemd service
unit.

pa-dlna runs as a `systemd/User`_ service unit (Pulseaudio and Pipewire run also
as a user service unit). Only one Control Point (such as pa-dlna) may interact
with a given DLNA device and pa-dlna enforces this rule by allowing only one
pa-dlna process per Sound Server.

.. list-table:: Systemd commands for pa-dlna
   :widths: 40 60
   :header-rows: 1

   * - Purpose
     - Command
   * - Enable pa-dlna and start it
     - ``systemctl --user enable --now pa-dlna``
   * - Disable pa-dlna and stop it
     - ``systemctl --user disable --now pa-dlna``
   * - Start pa-dlna
     - ``systemctl --user start pa-dlna``
   * - Stop pa-dlna
     - ``systemctl --user stop pa-dlna``
   * - Get the state of pa-dlna
     - ``systemctl --user status pa-dlna``
   * - Print the journal of pa-dlna
     - ``journalctl --user -u pa-dlna``

The pa-dlna.service unit
------------------------

The ``pa-dlna.service`` unit file is located in the ``systemd`` directory at the
root of the pa-dlna git repository.

Its content is:

.. include:: ../../systemd/pa-dlna.service
    :code: text

.. _python-systemd: https://www.freedesktop.org/software/systemd/python-systemd/
.. _systemd/User: https://wiki.archlinux.org/title/Systemd/User
