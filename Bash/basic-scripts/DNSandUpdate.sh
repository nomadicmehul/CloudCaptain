#!/bin/bash

echo "Change DNS"
echo
#sh -c "echo nameserver 77.88.8.88 > /etc/resolv.conf"
#sh -c "echo nameserver 77.88.8.2 >> /etc/resolv.conf"
echo
echo
#echo "Done! Your resolv.conf file should look like this:"
echo
echo
cat /etc/resolv.conf
echo "Update and Upgrade"
echo
sh -c "apt-get update && apt-get -y upgrade && apt-get -y dist-upgrade"
echo
echo "Done!"
