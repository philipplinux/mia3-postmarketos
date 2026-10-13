#!/bin/sh
# respawn.sh CMD...: keep CMD running (sway/config). Waits while sway has no output: when another VT is
# shown (dashboard, fbcon) new clients exit at once, so a plain restart loop would spin.
while :; do
    swaymsg -t get_outputs -r | grep -q '"name"' && "$@"
    sleep 2
done
