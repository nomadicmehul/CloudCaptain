#!/bin/bash

pidof java && kill `pidof java`

grep yacy /etc/passwd || useradd -m -s /bin/bash -d /opt/yacy yacy

# get java8
test -f /etc/ssl/certs/java/cacerts || apt-get install ca-certificates-java -y
apt-get remove default-jre ca-certificates-java openjdk-7-jre openjdk-7-jre-headless -y
apt-get autoremove

cd /opt/
rm -rf java* jre*
wget "http://javadl.oracle.com/webapps/download/AutoDL?BundleId=211989" -O /opt/java8.tar.gz
tar xfvz java8.tar.gz
cd jre1.8*/bin
ln -sfv "`pwd`/java" /usr/bin/java
cd ../lib/security
test -f /etc/ssl/certs/java/cacerts && ln -sfv /etc/ssl/certs/java/cacerts "`pwd`/cacerts" || echo "ca-certificates-java not found"

# setup yacy
cd /opt/yacy
test -e DATA || mkdir -v DATA
wget http://yacy.net/release/yacy_v1.90_20160704_9000.tar.gz -O yacy.tar.gz
tar xfvz yacy.tar.gz
cd yacy
ln -s /opt/yacy/DATA /opt/yacy/yacy/DATA
chmod +x /opt/yacy/yacy/startYACY.sh
chmod +x /opt/yacy/yacy/stopYACY.sh
chmod +x /opt/yacy/yacy/bin/passwd.sh
chown yacy /opt/yacy -R
chmod 700 /opt/yacy
ln -sfv "/opt/yacy/DATA/LOG/yacy00.log" "/opt/yacy/daemon.log"

# start yacy
pidof java || su -c "/opt/yacy/yacy/startYACY.sh" yacy
pidof java || sudo -u yacy /opt/yacy/yacy/startYACY.sh


# set yacy password
/opt/yacy/yacy/bin/passwd.sh PASSWORDHERE

# stop yacy
/opt/yacy/yacy/stopYACY.sh
