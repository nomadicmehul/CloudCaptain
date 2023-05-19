#!/bin/bash

if [ -s /etc/selinux/config ]; then
sed -i 's/SELINUX=enforcing/SELINUX=disabled/g' /etc/selinux/config
fi
platform=`uname -i`
if [ $platform = "x86_64" ]; then
  sysinfo="x86-64"
  else
   sysinfo="x86"
fi
if [ $platform = "unknown" ]; then
  platform="i386"
