#!/bin/bash
#
# PALI : The Perfect Automatic Lamp Installer
# Version : 2.3 (stable for use in Production)
# Author : Christophe Casalegno / Brain 0verride
# Website : http://www.christophe-casalegno.com
# Twitter : https://twitter.com/Brain0verride
# Email : brain@christophe-casalegno.com
# Note : Only tested on Debian 9


# Vars
RED='\033[38;5;160m' #ex echo -e "${RED} ALERT"
NC='\033[0m'	 	 #ex echo -e "${NC} Normal"
GREEN='\033[0;32m' 	 #ex echo -e "${GREEN} OK"
YELLOW='\033[38;5;226m' #ex echo -e "${YELLOW} Warning"
admin_mail='pali@christophe-casalegno.com' 	# Put your admin email here
installer='apt' 			# installer (apt-get, yum, urpmi, zypper, etc.)
installer_options='-y -f install' 	# options for the installer1
replacer='sed' 				# command used for replacement 
replacer_options='-i' 			# options for the replacer
sources_list='/etc/apt/sources.list'
myhost=$(hostname -f |cut -d " " -f1)
export HOSTNAME2=$myhost
ip=$(hostname -I |cut -d " " -f1)
auto='debconf-set-selections'
iptablesfix='/etc/network/if-pre-up.d/iptables'
firewall_conf='/etc/iptables.rules'
sshd_conf='/etc/ssh/sshd_config'
vhosts_conf='/etc/apache2/sites-available'
event_conf='/etc/apache2/mods-available/mpm_event.conf'
apache_conf='/etc/apache2/apache2.conf'
fpm_conf='/etc/php/7.0/fpm/pool.d'
php_conf='/etc/php/7.0/fpm/php.ini'
phpcli_conf='/etc/php/7.0/cli/php.ini'
phpmyadmin_conf='/etc/phpmyadmin/apache.conf'
mysql_conf='/etc/mysql/my.cnf'
munin_conf='/etc/munin/apache24.conf'
munin_master_conf='/etc/munin/munin.conf'
munin_node_conf='/etc/munin/munin-node.conf'
munin_orig='/usr/share/munin/plugins'
munin_dest='/etc/munin/plugins'
rkhunter_conf='/etc/default/rkhunter'
fail2ban_conf='/etc/fail2ban/jail.conf'
webmin_conf='/etc/webmin/miniserv.conf'
webmin_port='10000'
ssh_port='22'
nodejs_version='7'
tmpcron='/root/tmpcron.txt'
need_packages='pwgen'
conf_locale=$(set |grep LANG |cut -d "=" -f2)
options_found=0
letsencrypt=0
memory=$(free | awk 'FNR == 3 {print $4}' |awk '{ byte = $1 /1024/1024 ; byte =$1 /1024/2 ; print byte}' |cut -d "." -f1)

# UID verification

if [ "$UID" -ne "0" ]

then
        echo -e "${RED} [ ERROR ]" "${NC} you must be root to install the server"
        exit 0

else
        echo -e "${GREEN} [ OK ]" "${NC} UID ok, install in progress..."

fi



while getopts ":a:n:s:w:j:l:h" opt 
do

		options_found=1

		case $opt in
 
			a) 
				admin_mail="$OPTARG"  
				;;
			n) 
				myhost="$OPTARG"
				;;
			s) 
				ssh_port="$OPTARG"
				;;
			w) 
				webmin_port="$OPTARG"
				;;
			j)	
				nodejs_version="$OPTARG"
				;;
      l)
        letsencrypt="$OPTARG"
        ;;
			h) 
				echo './pali.sh -a your@email -n yourhostname (xxx.domain.tld) -s newssh_port -w newwebmin_port -j nodejs_version (6, 7 or 8) -l (1 for letsencrypt)'
				exit 1
				;;
			\?) 
				echo -e "${RED} [ ERROR ]" "${NC} Invalid option: -$OPTARG" >&2
				exit 1
				;;
			:) 
				echo -e "${YELLOW} [ WARNING ]" "Option -$OPTARG requires an argument." >&2
				exit 1
				;;

		esac
done



if [ "$options_found" -ne '1' ]

then

	echo -e "${YELLOW} [ WARNING ]" "${NC} no options found, defaults parameters will be used"

fi

#Verification if server was already installed

if [ -r "/root/$myhost.installed" ]

then

        echo -e "${RED} [ ERROR ]" "${NC} server $myhost was already installed !!!"
        exit 0

else
        echo -e "${GREEN} [ OK ]" "${NC} server has not been installed before, install in progress..."

fi


# Emails verifications

if [ "$admin_mail" = 'pali@christophe-casalegno.com' ]

then
	echo -e "${RED} [ ERROR ]" "${NC} admin email address has not been setuped, please setup it or use -a option"
	exit 0

else 
	echo -e "${GREEN} [ OK ]" "${NC} admin email ok, install in progress..."

fi

# Hostname verification

if [ "$myhost" != "$HOSTNAME2" ]

then
	echo -e "${YELLOW} [WARNING]" "${NC} Hostname doesn't match the actual server host, changing server hostname in progress..."
	oldhost=$(grep "$ip" /etc/hosts)
	cuthost=$(echo "$myhost" |cut -d "." -f1)
	newhost="$ip $myhost $cuthost"
  $replacer $replacer_options "s#$oldhost#$newhost#" /etc/hosts
	$replacer $replacer_options "s#$HOSTNAME#$cuthost#" /etc/hostname
	$replacer $replacer_options "s#root@$HOSTNAME#root@$cuthost#" /etc/ssh/*.pub
	hostname "$cuthost"
	hostnamectl set-hostname "$cuthost"
else

echo -e "${GREEN} [ OK ]" "${NC} hostname ok, install in progress..."

fi

# Fix warning: Falling back to a fallback locale issue
echo LC_ALL="$conf_locale" > /etc/environment
export_locale=$(grep LC_ALL /etc/environment)
export $export_locale

# Added sources
{
  echo '# Added by PALI' 
  echo '' 
  echo 'deb http://mirrors.linode.com/debian/ stretch main contrib non-free'
  echo 'deb-src http://mirrors.linode.com/debian/ stretch main contrib non-free'
  echo ''
  echo 'deb http://security.debian.org/ stretch/updates contrib non-free' 
  echo 'deb-src http://security.debian.org/ stretch/updates non-free' 
  echo ''
  echo 'deb http://mirrors.linode.com/debian/ stretch-updates main contrib non-free'
  echo 'deb-src http://mirrors.linode.com/debian/ stretch-updates main contrib non-free'
} >> $sources_list 

echo 'deb http://ftp.debian.org/debian stretch-backports main' | tee /etc/apt/sources.list.d/backports.list

$installer update
$installer -y upgrade

$installer $installer_options $need_packages

tld=$(pwgen -A -B 2 1)
usr=$(echo "$myhost" |sed 's/\.//g'|cut -b 1-14)
sysuser="$usr$tld"
genroot_pass=$(pwgen -A -B 12 1)
root_pass="$genroot_pass"

gensysuser_pass=$(pwgen -A -B 12 1)
sysuser_pass="$gensysuser_pass"

genmysqlroot_pass=$(pwgen -A -B 12 1)
mysqlroot_pass="$genmysqlroot_pass"

genmysql_pass=$(pwgen -A -B 12 1)
mysql_pass="$genmysql_pass"

genphpmyadmin_pass=$(pwgen -A -B 8 1)
phpmyadmin_pass="$genphpmyadmin_pass"

packages='libwww-perl ntpdate apt-transport-https python-certbot-apache man vim emacs iotop htop mc libapache2-mod-fcgid apache2 apache2-doc imagemagick php7.0 php7.0-zip php7.0-ssh2 php7.0-common php7.0-cli php7.0-mysqlnd php7.0-pgsql  php-apcu php7.0-curl php7.0-gd php7.0-intl php-imagick php7.0-imap php7.0-mcrypt php-memcache php-memcached php7.0-pspell php7.0-recode php7.0-sqlite3 php7.0-tidy php7.0-xmlrpc php7.0-xsl php-soap php7.0-mbstring php7.0-common php7.0-fpm mysql-server mysql-client phpmyadmin munin munin-node postfix libapache2-mod-php7.0 mailutils memcached git rsync pure-ftpd ftp curl strace python-setuptools python-dev gcc librsync-dev librsync1 python-cffi python-crypto python-cryptography python-ecdsa python-jwt python-lockfile python-ndg-httpsclient python-oauthlib python-openssl python-paramiko python-pkg-resources python-ply python-pyasn1 python-pycparser python-six python-urllib3 rkhunter chkrootkit fail2ban screen' # packages to be installed (you can modify for your needs) 

backport_packages=''

# Automation

# Mysql Server
$auto <<< "mysql-server mysql-server/root_password password $mysqlroot_pass"
$auto <<< "mysql-server mysql-server/root_password_again password $mysqlroot_pass"

# Postfix
$auto <<< "postfix postfix/mailname string $myhost"
$auto <<< "postfix postfix/main_mailer_type string 'Internet Site'"

# Phpmyadmin
$auto <<< "phpmyadmin phpmyadmin/dbconfig-install boolean true"
$auto <<< "phpmyadmin phpmyadmin/app-password-confirm password $phpmyadmin_pass"
$auto <<< "phpmyadmin phpmyadmin/mysql/admin-pass password $mysqlroot_pass"
$auto <<< "phpmyadmin phpmyadmin/mysql/app-pass password $phpmyadmin_pass"
$auto <<< "phpmyadmin phpmyadmin/reconfigure-webserver multiselect apache2"


# Content for file or other stuff 

content_firewall="
*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT DROP [0:0]
-A INPUT -m state --state RELATED,ESTABLISHED -j ACCEPT
-A INPUT -i lo -j ACCEPT
-A INPUT -p icmp -j ACCEPT
-A INPUT -p tcp -m tcp --dport $ssh_port -j ACCEPT
-A INPUT -p tcp -m tcp --dport 80 -j ACCEPT
-A INPUT -p tcp -m tcp --dport 443 -j ACCEPT
-A INPUT -p tcp -m tcp --dport $webmin_port -j ACCEPT
-A INPUT -p tcp --dport 20 -j ACCEPT
-A INPUT -p tcp --dport 21 -j ACCEPT
-A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
-A OUTPUT -m state --state RELATED,ESTABLISHED -j ACCEPT
-A OUTPUT -o lo -j ACCEPT
-A OUTPUT -p icmp -j ACCEPT
-A OUTPUT -p tcp -m tcp --dport 53 -j ACCEPT
-A OUTPUT -p udp -m udp --dport 53 -j ACCEPT
-A OUTPUT -p tcp -m tcp --dport 80 -j ACCEPT
-A OUTPUT -p tcp -m tcp --dport 22 -j ACCEPT
-A OUTPUT -p tcp -m tcp --dport 123 -j ACCEPT
-A OUTPUT -p udp -m udp --sport 123 -j ACCEPT
-A OUTPUT -p tcp -m tcp --dport 443 -j ACCEPT
-A OUTPUT -p tcp -m tcp --dport 25 -j ACCEPT
-A OUTPUT -p tcp -m tcp --dport 22 -j ACCEPT
-A OUTPUT -p tcp -m tcp --dport 65022 -j ACCEPT
COMMIT
"

content_munin='
Alias /munin /var/cache/munin/www
<Directory /var/cache/munin/www>
AuthUserfile /etc/munin/munin-htpasswd
AuthName "Munin Access"
AuthType Basic
Require valid-user
Options None
</Directory>

ScriptAlias /munin-cgi/munin-cgi-graph /usr/lib/munin/cgi/munin-cgi-graph
<Location /munin-cgi/munin-cgi-graph>
AuthUserfile /etc/munin/munin-htpasswd
AuthName "Munin Access"
AuthType Basic
Require valid-user

<IfModule mod_fcgid.c>
            SetHandler fcgid-script
        </IfModule>
        <IfModule !mod_fcgid.c>
            SetHandler cgi-script
        </IfModule>
</Location>
'


content_fail2ban="
[INCLUDES]
before = paths-debian.conf

[DEFAULT]

ignoreip = 127.0.0.1/8
ignorecommand =
bantime  = 600
findtime = 600
maxretry = 3
backend = auto
enabled = false
usedns = warn
logencoding = auto
filter = %(__name__)s
destemail = $admin_mail
sendername = Fail2Ban $myhost
sender = fail2ban@$myhost
mta = sendmail
protocol = tcp
chain = INPUT
port = 1:65535
fail2ban_agent = Fail2Ban/%(fail2ban_version)s
banaction = iptables-multiport
banaction_allports = iptables-allports
action_ = %(banaction)s[name=%(__name__)s, port=\"%(port)s\", protocol=\"%(protocol)s\", chain=\"%(chain)s\"]
action_mw = %(banaction)s[name=%(__name__)s, port=\"%(port)s\", protocol=\"%(protocol)s\", chain=\"%(chain)s\"]
              %(mta)s-whois[name=%(__name__)s, dest=\"%(destemail)s\", protocol=\"%(protocol)s\", chain=\"%(chain)s\", sendername=\"%(sendername)s\"]
action_mwl = %(banaction)s[name=%(__name__)s, port=\"%(port)s\", protocol=\"%(protocol)s\", chain=\"%(chain)s\"]
               %(mta)s-whois-lines[name=%(__name__)s, dest=\"%(destemail)s\", logpath=%(logpath)s, chain=\"%(chain)s\", sendername=\"%(sendername)s\"]
action = %(action_)s

[ssh]

enabled  = true
port     = $ssh_port
filter   = sshd
logpath  = /var/log/auth.log
maxretry = 6

[ssh-ddos]

enabled  = false
port     = $ssh_port
filter   = sshd-ddos
logpath  = /var/log/auth.log
maxretry = 6

[apache]

enabled  = true
port     = http,https
filter   = apache-auth
logpath  = /var/log/apache2/*error.log
maxretry = 6

[apache-multiport]

enabled   = true
port      = http,https
filter    = apache-auth
logpath   = /var/log/apache2/*error.log
maxretry  = 6

[apache-noscript]

enabled  = true
port     = http,https
filter   = apache-noscript
logpath  = /var/log/apache2/*error.log
maxretry = 6

[apache-overflows]

enabled  = true
port     = http,https
filter   = apache-overflows
logpath  = /var/log/apache2/*error.log
maxretry = 2

[apache-modsecurity]

enabled  = false
filter   = apache-modsecurity
port     = http,https
logpath  = /var/log/apache*/*error.log
maxretry = 2

[apache-nohome]

enabled  = false
filter   = apache-nohome
port     = http,https
logpath  = /var/log/apache*/*error.log
maxretry = 2

[pure-ftpd]

enabled  = true
port     = ftp,ftp-data,ftps,ftps-data
filter   = pure-ftpd
logpath  = /var/log/syslog
maxretry = 6

[postfix]

enabled  = false
port     = smtp,ssmtp,submission
filter   = postfix
logpath  = /var/log/mail.log

[recidive]

enabled  = false
filter   = recidive
logpath  = /var/log/fail2ban.log
action   = iptables-allports[name=recidive]
           sendmail-whois-lines[name=recidive, logpath=/var/log/fail2ban.log]
bantime  = 604800  ; 1 week
findtime = 86400   ; 1 day
maxretry = 5

[ssh-blocklist]

enabled  = false
filter   = sshd
action   = iptables[name=SSH, port=ssh, protocol=tcp]
           sendmail-whois[name=SSH, dest=\"%(destemail)s\", sender=\"%(sender)s\", sendername=\"%(sendername)s\"]
           blocklist_de[email=\"%(sender)s\", apikey=\"xxxxxx\", service=\"%(filter)s\"]
logpath  = /var/log/sshd.log
maxretry = 20
"

content_cron='
0 1 * * * /usr/sbin/ntpdate fr.pool.ntp.org
'

content_event='
<IfModule mpm_event_module>
  ServerLimit         256
  StartServers        50
  MaxClients          1024
  MinSpareThreads     50
  MaxSpareThreads     150
  ThreadsPerChild     40
  MaxRequestsPerChild 0
</IfModule>
'

content_firstvhost="
<VirtualHost $ip:80>
ServerAdmin webmaster@$myhost
DocumentRoot /home/$sysuser/www
ServerName $myhost
CustomLog /var/log/apache2/$myhost.log combined
ErrorLog /var/log/apache2/$myhost-error.log
ScriptAlias /cgi-bin/ /home/$sysuser/cgi-bin/

<FilesMatch \.php$>
SetHandler \"proxy:unix:/run/php/$myhost.sock|fcgi://localhost/\"
</FilesMatch>

        <Directory /home/$sysuser>
                Options -Indexes +FollowSymLinks +MultiViews
                AllowOverride All
                Order allow,deny
                allow from all
                Require all granted
        </Directory>

</VirtualHost>
"
if [ "$letsencrypt" = "1" ]

then

content_secondvhost="
<VirtualHost $ip:443>
ServerAdmin webmaster@$myhost
DocumentRoot /home/$sysuser/www
ServerName $myhost
CustomLog /var/log/apache2/$myhost.log combined
ErrorLog /var/log/apache2/$myhost-error.log
ScriptAlias /cgi-bin/ /home/$myhost/cgi-bin/

<FilesMatch \.php$>
SetHandler \"proxy:unix:/run/php/$myhost.sock|fcgi://localhost/\"
</FilesMatch>

        <Directory /home/$sysuser>
                Options Indexes FollowSymLinks MultiViews
                AllowOverride All
                Order allow,deny
                allow from all
                Require all granted
        </Directory>

SSLCertificateFile /etc/letsencrypt/live/$myhost/fullchain.pem
SSLCertificateKeyFile /etc/letsencrypt/live/$myhost/privkey.pem
Include /etc/letsencrypt/options-ssl-apache.conf

</VirtualHost>
"

fi

content_fpm_www="
[www]

user = www-data
group = www-data

listen = /run/php/www.sock
listen.backlog = -1
listen.allowed_clients = 127.0.0.1

listen.owner = www-data
listen.group = www-data
listen.mode = 0660

pm = dynamic
pm.max_children = 15
pm.start_servers = 6
pm.min_spare_servers = 3
pm.max_spare_servers = 9
pm.max_requests = 0

request_terminate_timeout = 0
request_slowlog_timeout = 0
 
slowlog = /var/log/php-fpm/www-slow.log
chdir = /
catch_workers_output = no
"

content_fpm="
[$myhost]

listen = /run/php/$myhost.sock

listen.backlog = -1
 
listen.allowed_clients = 127.0.0.1

listen.owner = $sysuser
listen.group = www-data
listen.mode = 0660

user = $sysuser
group = users

pm = dynamic
pm.max_children = 150
pm.start_servers = 25
pm.min_spare_servers = 25
pm.max_spare_servers = 75
pm.max_requests = 0

pm.status_path = /phpfpm-status-$myhost
ping.path = /phpfpm-ping-$myhost
ping.response = pong
 
request_terminate_timeout = 0
request_slowlog_timeout = 0
 
slowlog = /var/log/php-fpm/$myhost-slow.log
chdir = /
catch_workers_output = no
"

content_mycnf='
[client]
port            = 3306
socket          = /var/run/mysqld/mysqld.sock

[mysqld_safe]
socket          = /var/run/mysqld/mysqld.sock
nice            = 0

[mysqld]
user            = mysql
pid-file        = /var/run/mysqld/mysqld.pid
socket          = /var/run/mysqld/mysqld.sock
port            = 3306
basedir         = /usr
datadir         = /var/lib/mysql
tmpdir          = /tmp
lc-messages-dir = /usr/share/mysql
skip-external-locking
bind-address            = *
key_buffer_size         = 256M
max_allowed_packet      = 64M
thread_stack            = 192K
thread_cache_size       = 256
myisam-recover         = BACKUP
query_cache_limit       = 1M
query_cache_size        = 128M
max_connections         = 128
connect_timeout         = 10
wait_timeout            = 600
table_cache = 4096
table_open_cache = 4096
table_definition_cache = 4096
max_heap_table_size = 512M
sort_buffer_size        = 32M
bulk_insert_buffer_size = 16M
tmp_table_size          = 512M


log_error = /var/log/mysql/error.log
expire_logs_days        = 10
max_binlog_size         = 100M

innodb_buffer_pool_size = 2048M
innodb_log_buffer_size  = 16M
innodb_file_per_table   = 1
innodb_open_files       = 400
innodb_io_capacity      = 400
innodb_read_io_threads=64
innodb_write_io_threads=64
innodb_thread_concurrency=16
innodb_flush_method     = O_DIRECT
innodb_flush_log_at_trx_commit=2

[mysqldump]
quick
quote-names
max_allowed_packet      = 16M

[mysql]

[isamchk]
key_buffer_size         = 16M

!includedir /etc/mysql/conf.d/
'

content_genmysql="
CREATE USER '$sysuser'@'localhost' IDENTIFIED BY '$mysql_pass';
CREATE DATABASE IF NOT EXISTS \`"$sysuser"\` ;
GRANT ALL PRIVILEGES ON \`"$sysuser"\`.* TO '$sysuser'@'localhost';
FLUSH PRIVILEGES;
"

content_rootmysql="
UPDATE user set password=PASSWORD('$mysqlroot_pass') where User='root';
FLUSH PRIVILEGES;
"

echo "Making skel..."

mkdir /etc/skel/www
mkdir /etc/skel/cgi-bin
mkdir /etc/skel/protected
mkdir /root/mails
mkdir /root/sites
useradd "$sysuser" --shell /bin/bash -g users -m -d /home/"$sysuser" 
echo -e "$sysuser_pass\n$sysuser_pass" |passwd "$sysuser"


# SSH Configuration

echo "sshd configuration"
mkdir /root/.ssh
chmod 700 /root/.ssh
cd /root/.ssh

$replacer $replacer_options "s/#Port 22/Port 22/" $sshd_conf
$replacer $replacer_options "s#Port 22#Port $ssh_port#" $sshd_conf
$replacer $replacer_options "s/#PermitRootLogin/PermitRootLogin/" $sshd_conf 
$replacer $replacer_options "s#PermitRootLogin prohibit-password#PermitRootLogin yes#" $sshd_conf
cd /root

# Packages installation
echo "Installing packages : $packages..."
$installer $installer_options $packages
$installer $installer_options $backport_packages -t stretch-backports

# Composer Installation
echo "Installing composer..."
curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer

# NodeJs Installation
echo "Installing NodeJS..."
curl -sL https://deb.nodesource.com/setup_"$nodejs_version".x | bash -
$installer $installer_options nodejs
$installer $installer_options build-essential

# Grunt installation
echo "Installing grunt..."
npm install -g grunt-cli

# Drush 8.x installation
echo "Installing drush..."
php -r "readfile('https://s3.amazonaws.com/files.drush.org/drush.phar');" > /usr/bin/drush
chmod +x /usr/bin/drush

# Wp-cli installation
echo "Installing wp-cli"
curl https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar -L -o /usr/bin/wp
chmod +x /usr/bin/wp

# Drupal console installation
curl https://drupalconsole.com/installer -L -o drupal.phar
mv drupal.phar /usr/bin/drupal
chmod +x /usr/bin/drupal 

# Fix mandb bug
mandb

# Rkhunter configuration

$replacer $replacer_options "s#root#$admin_mail#" $rkhunter_conf
$replacer $replacer_options "s#APT_AUTOGEN=\"false\"#APT_AUTOGEN=\"true\"#g" $rkhunter_conf


# Fail2ban configuration

cat >$fail2ban_conf <<EOF
$content_fail2ban
EOF

# Modules activation
echo "Activating modules"
a2enmod userdir 
a2enmod rewrite
a2enmod ssl
a2enmod expires
a2enmod deflate
a2enmod headers
a2enmod actions
a2enmod proxy_fcgi setenvif
a2enconf php7.0-fpm

# Apache configuration

$replacer $replacer_options "s#Timeout 300#Timeout 30#" $apache_conf
$replacer $replacer_options "s#KeepAlive On#KeepAlive Off#" $apache_conf


cat >$event_conf <<EOF
$content_event
EOF

# Vhost configuration

cat >$vhosts_conf/"$myhost".conf <<EOF
$content_firstvhost
EOF

cat >$vhosts_conf/"$myhost".ssl.conf <<EOF
$content_secondvhost
EOF

# Vhost activation
a2ensite "$myhost.conf"

if [ "$letsencrypt" = "1" ]

  then
      a2ensite default-ssl.conf
      certbot -n --agree-tos --email "$admin_mail" --apache certonly --domains "$myhost"
      a2ensite "$myhost".ssl.conf 

  else
      echo "no SSL"
      
fi

# FPM pool configuration
cat  >$fpm_conf/"$myhost".conf <<EOF
$content_fpm
EOF

cat >$fpm_conf/www.conf <<EOF
$content_fpm_www
EOF


# Mysql Creation
echo >/root/root.sql
cat >/root/root.sql <<EOF
$content_rootmysql
EOF

mysql -u root --database=mysql < /root/root.sql

echo >/root/sql.sql
cat >/root/sql.sql <<EOF
$content_genmysql
EOF

mysql -u root -p"$mysqlroot_pass" < /root/sql.sql

# Php configuration

#fpm

$replacer $replacer_options "s#memory_limit = 128M#memory_limit = 512M#" $php_conf
$replacer $replacer_options "s#;upload_tmp_dir =#upload_tmp_dir =/tmp#" $php_conf
$replacer $replacer_options "s#upload_max_filesize = 2M#upload_max_filesize = 128M#" $php_conf
$replacer $replacer_options "s#post_max_size = 8M#post_max_size = 128M#" $php_conf
$replacer $replacer_options "s#max_file_uploads = 20#max_file_uploads = 128#" $php_conf
$replacer $replacer_options "s#max_execution_time = 30#max_execution_time = 600#" $php_conf
$replacer $replacer_options "s#max_input_time = 60#max_input_time = 600#" $php_conf
$replacer $replacer_options "s#; max_input_vars = 100#max_input_vars = 100000#" $php_conf

#cli

$replacer $replacer_options "s#memory_limit = 128M#memory_limit = 512M#" $phpcli_conf
$replacer $replacer_options "s#;upload_tmp_dir =#upload_tmp_dir =/tmp#" $phpcli_conf
$replacer $replacer_options "s#upload_max_filesize = 2M#upload_max_filesize = 128M#" $phpcli_conf
$replacer $replacer_options "s#post_max_size = 8M#post_max_size = 128M#" $phpcli_conf
$replacer $replacer_options "s#max_file_uploads = 20#max_file_uploads = 128#" $phpcli_conf
$replacer $replacer_options "s#max_execution_time = 30#max_execution_time = 600#" $phpcli_conf
$replacer $replacer_options "s#max_input_time = 60#max_input_time = 600#" $phpcli_conf
$replacer $replacer_options "s#; max_input_vars = 100#max_input_vars = 100000#" $phpcli_conf
$replacer $replacer_options "s#session.save_handler = files#session.save_handler = memcached#" $phpcli_conf
$replacer $replacer_options "s#;session.save_path = \"/var/lib/php7.0/sessions\"#session.save_path = \"localhost:11211\"#" $phpcli_conf

# Pure-ftpd configuration
$replacer $replacer_options "s#no#yes#" /etc/pure-ftpd/auth/65unix
$replacer $replacer_options "s#yes#no#" /etc/pure-ftpd/auth/70pam
$replacer $replacer_options "s#no#yes#" /etc/pure-ftpd/conf/UnixAuthentication
$replacer $replacer_options "s#yes#no#" /etc/pure-ftpd/conf/PAMAuthentication
$replacer $replacer_options "s#1000#33#" /etc/pure-ftpd/conf/MinUID

# Pure-ftpd : no TLS for the moment (firewall issue)
echo 0 > /etc/pure-ftpd/conf/TLS 
openssl req -x509 -nodes -days 3650 -newkey rsa:4096 -sha256 -keyout /etc/ssl/private/pure-ftpd.pem -out /etc/ssl/private/pure-ftpd.pem -subj "/C=IE/ST=Co. Mayo/L=PALI/O=PALI/CN=$myhost"

touch /etc/pure-ftpd/conf/ChrootEveryone
echo "yes" > /etc/pure-ftpd/conf/ChrootEveryone
echo "ip_conntrack_ftp" >> /etc/modules
modprobe ip_conntrack_ftp

#Munin configuration 

cat >$munin_conf <<EOF
$content_munin
EOF

$replacer $replacer_options "s/#host_name localhost.localdomain/host_name $myhost/" $munin_node_conf
$replacer $replacer_options "s#localhost.localdomain#$myhost#" $munin_master_conf

ln -s $munin_orig/mysql_ $munin_dest/mysql_
ln -s $munin_orig/mysql_bytes $munin_dest/mysql_bytes
ln -s $munin_orig/mysql_innodb $munin_dest/mysql_innodb
ln -s $munin_orig/mysql_isam_space_ $munin_dest/mysql_isam_space_
ln -s $munin_orig/mysql_queries $munin_dest/mysql_queries
ln -s $munin_orig/mysql_slowqueries $munin_dest/mysql_slowqueries
ln -s $munin_orig/mysql_threads $munin_dest/mysql_threads
ln -s $munin_orig/postfix_mailstats $munin_dest/postfix_mailstats

htpasswd -b -c /etc/munin/munin-htpasswd "$sysuser" "$sysuser_pass" 

# Mysql configuration
cat >$mysql_conf <<EOF
$content_mycnf
EOF

# Phpmyadmin configuration
echo "
<FilesMatch \.php$>
SetHandler \"proxy:unix:/run/php/www.sock|fcgi://localhost/\"
</FilesMatch>
" >> $phpmyadmin_conf

cat /var/lib/phpmyadmin/blowfish_secret.inc.php  |grep cfg >> /etc/phpmyadmin/config.inc.php

$replacer $replacer_options "s#innodb_buffer_pool_size = 2048M#innodb_buffer_pool_size="$memory"M#" $mysql_conf

echo '<?php echo "test ok";?>' > /home/"$sysuser"/www/index.php
echo '<?php phpinfo();?>' > /home/"$sysuser"/www/phpinfo.php

# Fix perms
chown -R "$sysuser":users /home/"$sysuser"
chmod 705 /home/"$sysuser"

# Webmin installation

wget http://www.webmin.com/jcameron-key.asc
apt-key add jcameron-key.asc
{
  echo '# Added by PALI' 
  echo ''
  echo 'deb http://download.webmin.com/download/repository sarge contrib'
  echo 'deb http://webmin.mirror.somersettechsolutions.co.uk/repository sarge contrib' 
} >> $sources_list

$installer update
$installer $installer_options webmin
$replacer $replacer_options "s#10000#$webmin_port#" $webmin_conf

# Restart and activate everything needed

service apache2 restart
systemctl restart php7.0-fpm.service
service mysql restart
service ssh restart
service pure-ftpd restart
service webmin restart
service fail2ban restart

# Installation des cron

crontab -l > $tmpcron
cat>>$tmpcron <<EOF
$content_cron
EOF

crontab $tmpcron
rm -f $tmpcron

touch /root/"$myhost".installed

echo -e "$root_pass\n$root_pass" |passwd root

echo "$mysqlroot_pass" > /root/.mysqlpasswd

content_mail="
Your server has been correclty installed

Hostname : $myhost
IP address : $ip

ssh port : $ssh_port
Webmin : https://$myhost:$webmin_port

Phpmyadmin : http://$myhost/phpmyadmin

Munin : http://$myhost/munin

Credentials :
---------------------------------------
SSH : root : $root_pass
SSH/FTP : $sysuser : $sysuser_pass
MysqlRoot : $mysqlroot_pass

Mysql : 
login : $sysuser 
database : $sysuser 
password : $mysql_pass 
"

cat >/root/mails/installed.txt <<EOF
$content_mail
EOF

cat >/root/.p <<EOF
$content_mail
EOF

mail -s "[ $myhost ] installed" "$admin_mail" < /root/mails/installed.txt
cat /root/mails/installed.txt > /root/.p 

echo > /root/mails/installed.txt

cat >/root/sites/"$myhost".installed <<EOF
$content_mail
EOF

# Firewall configuration

cat >$firewall_conf <<EOF
$content_firewall
EOF

iptables-restore <$firewall_conf
iptables-save > /etc/iptables.up.rules

touch $iptablesfix
echo "#!/bin/sh" > $iptablesfix
echo "/sbin/iptables-restore < /etc/iptables.up.rules" >> $iptablesfix
chmod +x $iptablesfix 

iptables -A INPUT -p tcp -m tcp --dport 22 -j ACCEPT

exit 0

