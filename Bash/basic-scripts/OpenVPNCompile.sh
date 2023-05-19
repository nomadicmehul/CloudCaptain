#!/bin/bash

latest=2.3.14
aptitude update && aptitude full-upgrade && aptitude install openvpn ca-certificates build-essential liblzo2-dev libssl-dev libpam0g-dev -y
wget https://swupdate.openvpn.net/community/releases/openvpn-$latest.tar.gz -O /usr/src/openvpn-$latest.tar.gz
cd /usr/src; tar xfvz openvpn-$latest.tar.gz && cd openvpn-$latest && ./configure && make && make install
sed -i 's/DAEMON=\/usr\/sbin\/openvpn/DAEMON=\/usr\/local\/sbin\/openvpn/g' /etc/init.d/openvpn
aptitude remove openvpn -y
which openvpn && openvpn --version | head -1

/etc/init.d/openvpn restart || /etc/init.d/openvpn start
