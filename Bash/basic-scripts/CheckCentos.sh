#!/bin/bash

if [ ! -f /etc/redhat-release ]; then
  echo "Only supports Centos"
  exit 1
fi
