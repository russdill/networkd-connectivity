#!/bin/sh
# Note: The ddclient should be configured to obtain the outgoing source
# IP address:
#     use=cmd, cmd="ip route get 8.8.8.8 | sed -n 's/.* src \\([0-9.]\\+\\).*/\\1/p'"
# >= 4.0.0 should use usev4=cmdv4, cmdv4=...
which -s ddclient && [ -e /etc/ddclient.conf ] && ddclient -daemon=0
