#! /bin/bash
# Start pipewire-pulse and run the test suite.

export XDG_RUNTIME_DIR=/tmp
pipewire &
echo "*** pipewire started"

pipewire-pulse &
echo "*** pipewire-pulse started"

# wireplumber needs an X11 server.
export DISPLAY=:0.0
Xvfb -screen $DISPLAY 1920x1080x24 &
echo "*** Xvfb started"

wireplumber &
echo "*** wireplumber started"

sleep 1
pw-cli help
pactl info
python3 -m unittest --verbose --failfast
