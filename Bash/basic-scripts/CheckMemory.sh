#!/bin/bash

phymem="$(free | awk '/^Mem:/{print $2}')"
[ -z "$phymem" ] && phymem=0
if [ "$phymem" -lt 1000000 ]; then
  echoerr "A minimum of 1024 MB RAM is required."
  exit 1
fi
