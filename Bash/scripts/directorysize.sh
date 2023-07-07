#!/bin/bash

echo -n "Enter your directory: "
read -r x
du -sh "$x"
