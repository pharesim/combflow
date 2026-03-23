#!/bin/sh
mkdir -p /var/www/goaccess
while true; do
  awk '{
    ip="";ts="";m="";u="";H="";s="";b="";ua="-";v=""
    if(match($0,/"remote_ip":"[^"]+"/))ip=substr($0,RSTART+13,RLENGTH-14)
    if(match($0,/"ts":[0-9]+/))ts=substr($0,RSTART+5,RLENGTH-5)
    if(match($0,/"method":"[^"]+"/))m=substr($0,RSTART+10,RLENGTH-11)
    if(match($0,/"uri":"[^"]+"/))u=substr($0,RSTART+7,RLENGTH-8)
    if(match($0,/"proto":"[^"]+"/))H=substr($0,RSTART+9,RLENGTH-10)
    if(match($0,/"status":[0-9]+/))s=substr($0,RSTART+9,RLENGTH-9)
    if(match($0,/"size":[0-9]+/))b=substr($0,RSTART+7,RLENGTH-7)
    if(match($0,/"User-Agent":\["[^"]+"/))ua=substr($0,RSTART+15,RLENGTH-16)
    if(match($0,/"host":"[^"]+"/))v=substr($0,RSTART+8,RLENGTH-9)
    if(ip!=""&&ts!=""&&u!="/stats")printf "%s|%s|%s|%s|%s|%s|%s|%s|%s\n",ip,ts,m,u,H,s,b,ua,v
  }' /var/log/caddy/access.log | \
  goaccess - \
    -o /var/www/goaccess/report.html \
    --datetime-format=%s \
    --log-format='%h|%x|%m|%U|%H|%s|%b|%u|%v' \
    2>/dev/null
  sleep 60
done
