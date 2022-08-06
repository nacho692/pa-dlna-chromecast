An UPnP control point that forwards Pulseaudio streams to DLNA devices.

**Work in progress**

Development
-----------

Asyncio tasks:

    upnp asyncio tasks:
      ssdp notify task              monitor reception of ssdp notify PDUs
      ssdp msearch task             send msearch PDUs at regular intervals
      n x root device aging tasks   implement control of the aging of a root device

    AVControlPoint asyncio tasks:
      main task                     * run the UPnPControlPoint that starts the upnp
                                      tasks above
                                    * create the pulse task, the http_server task
                                      and the renderers tasks
                                    * monitor upnp events
      pulse task                    monitor puseaudio sink-input events
      http_server task              serve DLNA requests
      n x renderer tasks            act upon pulseaudio events and monitor the
                                    stream task
      n x stream tasks              start/monitor the processes that stream audio
                                    from null-sink monitor to the http socket via
                                    'parec | encoder pgm (ffmpeg) | http socket'
