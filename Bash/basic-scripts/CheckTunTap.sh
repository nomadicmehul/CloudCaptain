#!/bin/bash

if [[ ! -e /dev/net/tun ]]; then
	echo "TUN is not available"
	exit 3
fi
