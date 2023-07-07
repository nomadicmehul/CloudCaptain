#!/usr/bin/env bash

x=0; y=1; i=2
while true; do
	i=$((i + 1))
	z=$((x + y))
	echo -n "$z "
	x=$y
	y=$z
	sleep .5
done
