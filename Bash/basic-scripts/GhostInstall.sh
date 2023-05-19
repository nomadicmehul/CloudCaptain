#!/bin/bash

clear

set -e

echo ""

echoerr() { echo "ERROR: ${1}" >&2; }

if [ "$(id -u)" != 0 ]; then
  echoerr "This script must be run as root. 'sudo bash $0'"
  exit 1
fi

os_type="$(lsb_release -si 2>/dev/null)"
if [ "$os_type" != "Ubuntu" ] && [ "$os_type" != "Debian" ]; then
  echoerr "Only supports Ubuntu/Debian"
  exit 1
fi

if [ "$os_type" = "Ubuntu" ]; then
  os_ver="$(lsb_release -sr)"
  if [ "$os_ver" != "16.04" ] && [ "$os_ver" != "14.04" ] && [ "$os_ver" != "12.04" ]; then
    echoerr "Only supports Ubuntu 12.04/14.04/16.04"
    exit 1
  fi
fi

if [ "$os_type" = "Debian" ]; then
  os_ver="$(sed 's/\..*//' /etc/debian_version 2>/dev/null)"
  if [ "$os_ver" != "8" ]; then
    echoerr "Only supports Debian 8 (Jessie)"
    exit 1
  fi
fi

phymem="$(free | awk '/^Mem:/{print $2}')"
[ -z "$phymem" ] && phymem=0
if [ "$phymem" -lt 1000000 ]; then
  echoerr "A minimum of 1024 MB RAM is required."
  exit 1
fi

echo "Please enter valid hostname:"
echo ""
read HOSTNAME

FQDN_REGEX='^(([a-zA-Z](-?[a-zA-Z0-9])*)\.)*[a-zA-Z](-?[a-zA-Z0-9])+\.[a-zA-Z]{2,}$'
if ! printf %s "$HOSTNAME" | grep -Eq "$FQDN_REGEX"; then
  echoerr "Invalid parameter. You must enter a FQDN domain name... exp: blog.mertcangokgoz.com"
  exit 1
fi

echo "System upgrade and install dependencies"
apt-get -y update
apt-get -y upgrade
apt-get install -y npm nodejs nodejs-legacy zip nginx
curl -sL https://deb.nodesource.com/setup_8.x | sudo bash -

echo "Ghost download and configuring"
mkdir -p /var/www
cd /var/www/
curl -L -O https://ghost.org/zip/ghost-latest.zip
unzip -d ghost ghost-latest.zip
rm ghost-latest.zip
cd ghost/
sed -e "s/my-ghost-blog.com/$HOSTNAME/" <config.example.js > config.js
npm install -g grunt-cli
npm install --production

echo "configuring ghost user"
adduser --shell /bin/bash --gecos 'Ghost application' ghost --disabled-password
echo ghost:ghost | chpasswd
chown -R ghost:ghost /var/www/ghost/

echo "configuring nginx"
touch /etc/nginx/sites-available/ghost
echo "server {" >> /etc/nginx/sites-available/ghost
echo "    listen 80;" >> /etc/nginx/sites-available/ghost
echo "    server_name $HOSTNAME;" >> /etc/nginx/sites-available/ghost
echo "    location / {" >> /etc/nginx/sites-available/ghost
echo "        proxy_set_header   X-Real-IP \$remote_addr;" >> /etc/nginx/sites-available/ghost
echo "        proxy_set_header   Host      \$http_host;" >> /etc/nginx/sites-available/ghost
echo "        proxy_pass         http://127.0.0.1:2368;" >> /etc/nginx/sites-available/ghost
echo "        }" >> /etc/nginx/sites-available/ghost
echo "    }" >> /etc/nginx/sites-available/ghost

ln -s /etc/nginx/sites-available/ghost /etc/nginx/sites-enabled/ghost

echo "remove default profile and restart nginx"
rm /etc/nginx/sites-available/default
rm /etc/nginx/sites-enabled/default
service nginx restart

echo "install PM2"
echo "#!/bin/bash" >> /home/ghost/start.sh
echo "export NODE_ENV=production" >> /home/ghost/start.sh
echo "cd /var/www/ghost/" >> /home/ghost/start.sh
echo "npm start --production" >> /home/ghost/start.sh
chmod +x /home/ghost/start.sh
npm install pm2 -g

echo "configuring PM2"
su -c "echo 'export NODE_ENV=production' >> ~/.profile" -s /bin/bash ghost
su -c "source ~/.profile" -s /bin/bash ghost
su -c "/usr/local/bin/pm2 kill" -s /bin/bash ghost
su -c "env /usr/local/bin/pm2 start /home/ghost/start.sh --interpreter=bash --name ghost" -s /bin/bash ghost
env PATH=$PATH:/usr/bin pm2 startup ubuntu -u ghost --hp /home/ghost
su -c "pm2 save" -s /bin/bash ghost

echo "Ghost CMS Started"
