#!/bin/bash

set -e

YAZICILAR=`lpstat -p | grep printer | grep -v enable | awk '{print $2}' | sed '/^$/d'`
if [ "x$YAZICILAR" !=  "x" ]; then
  for yaziciadi in $YAZICILAR; do
    echo "Lutfen Bekleyin $yaziciadi Yazici Etkinlestiriliyor"
    cupsenable -h 127.0.0.1:631 $yaziciadi && logger "$yaziciadi Yazici Etkinlestirildi"
  done
fi
sleep 2
