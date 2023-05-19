#!/bin/bash

echo "Please enter valid hostname:"
echo ""
read HOSTNAME

FQDN_REGEX='^(([a-zA-Z](-?[a-zA-Z0-9])*)\.)*[a-zA-Z](-?[a-zA-Z0-9])+\.[a-zA-Z]{2,}$'
if ! printf %s "$HOSTNAME" | grep -Eq "$FQDN_REGEX"; then
  echoerr "Invalid parameter. You must enter a FQDN domain name... exp: blog.mertcangokgoz.com"
  exit 1
fi
