An UPnP control point routing PulseAudio streams to DLNA devices.

**Work in progress**

Development
-----------

Asyncio tasks:

    UPnPControlPoint tasks:
      ssdp notify           Monitor reception of notify SSDPs.
      ssdp msearch          Send msearch SSDPs at regular intervals.
      n x root device       Implement control of the aging of an UPnP root
                            device.

    AVControlPoint tasks:
      main                  * Instantiate the UPnPControlPoint that starts the
                              UPnP tasks.
                            * Create the pulse task, the http_server task, the
                              renderer tasks and the shutdown task.
                            * Handle UPnP notifications.
      pulse                 Monitor pulseaudio sink-input events.
      http_server           Serve DLNA HTTP requests and start the
                            client_connected tasks.
      n x renderer          Act upon pulseaudio events and run UPnP soap
                            actions.
      shutdown              Wait on event pushed by the signal handlers.

    HTTPServer tasks:
      client_connected      HTTPServer callback wrapped by asyncio in a task.
                            Start the tasks that forward the audio stream
                            from a pulseaudio null-sink monitor to the HTTP
                            socket via 'parec | encoder program | HTTP socket'.

    StreamSession tasks:
      parec process         Start the parec process and wait for its exit.
      parec log_stderr      Log the parec process stderr.
      encoder process       Start the encoder process and wait for its exit.
      encoder log_stderr    Log the encoder process stderr.
      track                 Write the audio stream to the HTTP socket.

    Track tasks:
      abort                 Abort the pa-dlna program.
      shutdown              Write the last chunk and close the HTTP socket.

The '--networks' command line option:
    Accept IP interfaces or IP addresses in the its comma separated list.
    A renderer is instantiated upon receiving an 'alive' notification:
        * If the root device IP destination address exists (an UPnP msearch
          SSDP response) and this address matches one of these IP
          interfaces or IP addresses.
        * Otherwise (an UPnP notify SSDP broadcast) if the source IP
          address belongs to the network of one of these IP interfaces.
