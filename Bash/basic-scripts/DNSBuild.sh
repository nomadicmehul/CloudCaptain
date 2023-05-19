#!/bin/bash

echo "Welcome To Server DNS Creator"
echo "Enter the following configuration parameters of the DNS"
echo -n "Domain name: "; read -r domain
echo -n "Ip address for dns (127.0.0.1): "; read -r ip
echo -n "Ip address reverse zone (0.0.127) : "; read -r ipinv
echo -n "Address network (127.0.0.1/32): "; read -r addred
echo "Configuring DNS Please Wait..."
echo "

acl goodclients {
    $addred;
    localhost;
    localnets;
};

options {
	listen-on port 53 { 127.0.0.1; $ip;};
	listen-on-v6 port 53 { any; };
	directory 	\"/var/named\";
	dump-file 	\"/var/named/data/cache_dump.db\";
	statistics-file \"/var/named/data/named_stats.txt\";
	memstatistics-file \"/var/named/data/named_mem_stats.txt\";

	recursion yes;
	allow-query     { goodclients; };

	auth-nxdomain no;
	dnssec-enable yes;
	dnssec-validation yes;

	/* Path to ISC DLV key */
	bindkeys-file \"/etc/named.iscdlv.key\";

	managed-keys-directory \"/var/named/dynamic\";

	pid-file \"/run/named/named.pid\";
	session-keyfile \"/run/named/session.key\";
};

logging {
        channel default_debug {
                file \"data/named.run\";
                severity dynamic;
        };
};

zone \".\" IN {
	type hint;
	file \"named.ca\";
};

zone \"$domain\" IN {
	type master;
	file \"direct.${domain%.*}\";
	allow-update {none;};
};

zone \"$ipinv.in-addr.arpa\" IN {
	type master;
	file \"reverse.${domain%.*}\";
	allow-update {none;};
};

include \"/etc/named.rfc1912.zones\";
include \"/etc/named.root.key\";
" > /etc/named.conf
echo "[PASS]"
echo "Configuring zones"
echo -n "IP for pgsql.$domain: "; read -r ip1
echo -n "IP for aulavirtual.$domain: "; read -r ip2
echo -n "IP for mariadb.$domain: "; read -r ip3
echo -n "IP for system.$domain: "; read -r ip4
echo "\$TTL 86400
@ IN SOA www.$domain. root.$domain. (
 2009091001
 28800
 7200
 604800
 86400
 )
@ IN NS www.$domain.
@ IN A $ip
@ IN A $ip1
@ IN A $ip2
@ IN A $ip3
@ IN A $ip4
www IN A $ip
pgsql IN A $ip1
aulavirtual IN A $ip2
mariadb IN A $ip3
system IN A $ip4
" >  "/var/named/direct.${domain%.*}"
echo "\$TTL 86400
@ IN SOA www.$domain. root.$domain. (
 2009091001
 28800
 7200
 604800
 86400
 )
@ IN NS www.$domain.
@ IN PTR $domain.
www IN A $ip
pgsql IN A $ip1
aulavirtual IN A $ip2
mariadb IN A $ip3
system IN A $ip4

${ip##*.} IN PTR www.$domain.
${ip1##*.} IN PTR pgsql.$domain.
${ip2##*.} IN PTR aulavitual.$domain.com.
${ip3##*.} IN PTR mariadb.$domain.
${ip4##*.} IN PTR system.$domain.
" > "/var/named/reverse.${domain%.*}"
echo "[PASS]"
echo "Restarting named"
systemctl restart named
named-checkconf /etc/named.conf
named-checkzone "$domain" /var/named/direct."${domain%.*}"
named-checkzone "$domain" /var/named/reverse."${domain%.*}"
echo -e "\n nameserver $ip" >> /etc/resolve.conf
echo "Restarting httpd"
systemctl restart httpd
echo "[PASS]"
echo "DNS Configuration finished"
echo "to add a new one, run the script again"
echo "sh dnsbuild.sh"
echo "Thanks"
