#!/bin/sh

dpkg -l openvpn > /dev/null 2>&1
if [[ $? -eq 0 ]]; then
    die "❯❯❯ OpenVPN is already installed."
fi
