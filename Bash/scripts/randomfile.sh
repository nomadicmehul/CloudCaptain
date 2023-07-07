#!/usr/bin/env bash

echo "Hello $USER"
uptime >> "$(date)".txt
echo "Your File is being saved to $(pwd)"
