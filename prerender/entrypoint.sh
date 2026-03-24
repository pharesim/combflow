#!/bin/sh
# Start cleanup loop in background (runs every hour)
while true; do
  sleep 3600
  /home/user/cleanup.sh
done &

# Start prerender server
exec node /home/user/server.js
