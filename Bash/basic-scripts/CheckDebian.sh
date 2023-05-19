#!/bin/bash

if [ "$os_type" = "Debian" ]; then
  os_ver="$(sed 's/\..*//' /etc/debian_version 2>/dev/null)"
  if [ "$os_ver" != "8" ]; then
    echoerr "Only supports Debian 8 (Jessie)"
    exit 1
  fi
fi
