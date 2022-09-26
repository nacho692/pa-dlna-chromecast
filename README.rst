An UPnP control point that forwards Pulseaudio streams to DLNA devices.

**Work in progress**

Development
-----------

Asyncio tasks:

    UPnP tasks:
      ssdp notify task              monitor reception of ssdp notify PDUs
      ssdp msearch task             send msearch PDUs at regular intervals
      n x root device aging tasks   implement control of the aging of a root device

    AVControlPoint tasks:
      main task                     * run the UPnPControlPoint that starts the UPnP
                                      tasks above
                                    * create the pulse task, the http_server task
                                      and the renderer tasks
                                    * handle UPnP notifications
      shutdown task                 wait on event pushed by the signal handlers
      pulse task                    monitor puseaudio sink-input events
      http_server task              serve DLNA requests and start the streaming tasks
      n x renderer tasks            act upon pulseaudio events and run UPnP soap
                                    actions
      n x streaming tasks           start the processes that stream audio from a
                                    pulseaudio null-sink monitor to the http socket
                                    via 'parec | encoder program | http socket'

    Stream tasks:
      parec process
      encoder process
      log_stderr task
