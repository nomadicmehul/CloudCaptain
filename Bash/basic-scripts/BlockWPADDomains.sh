#!/bin/bash

mkdir /etc/dnsmasq;
URL=https://data.iana.org/TLD/tlds-alpha-by-domain.txt
test -f /etc/dnsmasq/block.wpad.txt && rm /etc/dnsmasq/block.wpad.txt
for DOM in `wget -q $URL -O- |grep -v "#"`; do
echo "server=/wpad.${DOM}/" >> /etc/dnsmasq/block.wpad.txt
done

# add to dnsmasq.conf:
servers-file=/etc/dnsmasq/block.wpad.txt
