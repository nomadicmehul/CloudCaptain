#!/usr/bin/env bash
printf "Enter the First Number: "
read -r a
printf "Enter the Second Number: "
read -r b
echo "$a - $b = $((a - b))"
