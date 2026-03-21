#!/bin/bash

if [ -f /proc/user_beancounters ]; then
  echo "Error: This script does not support OpenVZ VPS." >&2
  exit 1
fi
