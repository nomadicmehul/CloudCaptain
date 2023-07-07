#!/usr/bin/env bash

echo -n "Enter File Name: "
read -r file

if [ ! -f "$file" ]; then
	echo "Filename $file does not exists"
	exit 1
fi

tr '[:upper:]' '[:lower:]' < "$file" >> small.txt
