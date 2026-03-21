#!/usr/bin/env bash

while true; do
	rand=$(shuf -i 2600-2700 -n 1)
	echo -en "   \u$rand"
	sleep 1
done
