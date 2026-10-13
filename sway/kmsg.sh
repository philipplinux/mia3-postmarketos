#!/bin/sh
# tab tty1 (sway/config): live kernel log. buffyboard registers a new input device every time tab 4 opens; hide that.
journalctl -k -f -n 300 -o short-monotonic | awk '!/input: buffyboard as/ { print; fflush() }'
