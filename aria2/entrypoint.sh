#!/bin/sh
# Start aria2 with the RPC secret taken from the environment (never baked in).
set -e

mkdir -p /downloads /config

# aria2 refuses to start when input-file points to a missing file.
if [ ! -f /config/aria2.session ]; then
    : > /config/aria2.session
fi

exec aria2c --conf-path=/etc/aria2/aria2.conf --rpc-secret="${RPC_SECRET:-}"