#! /bin/bash

set -e

if [[ -z "$1" ]];
then
  echo "Please enter a website url"
  exit 1
else
  if [[ -z "$2" ]];
  then
    echo "Please enter a custom code"
    exit 1
  else
    echo "Please Wait..."
    curl -s -i https://git.io -F "url=$1" -F "code=$2" | awk '/Location/ {print $2}'
    fi
fi
