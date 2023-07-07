#!/usr/bin/env bash

#
# Author: Abhishek Shingane (abhisheks@iitbhilai.ac.in)
# Date: 11 Sep 2020
#

if ! [ -x "$(command -v jq)" ]; then
  echo 'Error: jq is not installed. Install via https://stedolan.github.io/jq/download/'
  exit 1
fi

if [[ $# -ne 1 ]]; then
	echo 'Provide I.P as command line parameter. Usage:  ' $0 ' 15.45.0.1 '
	exit 1
fi
link=$(echo "http://ip-api.com/json/"$1)
data=$(curl $link -s) # -s for slient output

status=$(echo $data | jq '.status' -r)

if [[ $status == "success" ]]; then
	
	city=$(echo $data | jq '.city' -r)
	regionName=$(echo $data | jq '.regionName' -r)
	country=$( echo $data | jq '.country' -r)
	echo $city, $regionName in $country. 
fi 


