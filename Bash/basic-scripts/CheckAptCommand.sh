#!/bin/bash

if [ -n "$(command -v apt-get | wc -l)" != "1" ]
then
	echo "Please use Debian based system"
	exit 1
fi
