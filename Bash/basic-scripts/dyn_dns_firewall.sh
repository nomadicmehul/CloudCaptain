#!/bin/bash

echo "A very simple utility to update a firewall with a dynamic IP address."
echo "Espcially useful when using a cron job."
echo -n "Hostname is $HOSTNAME";
HOSTNAME=mertcangokgoz.com
FIREWALL=iptables
IFACE=eth0
PROTO=tcp
PORT=22

LOGFILE=/var/dyn_firewall_${HOSTNAME}.ip

Current_IP=$(host $HOSTNAME | cut -f4 -d' ')

if [ ! -f $LOGFILE ] ; then
	case ${FIREWALL} in
	*)
	iptables*)
		iptables -I INPUT -i ${IFACE} -p ${PROTO} -s ${Current_IP} --dport ${PORT} -j ACCEPT
		;;

	ufw*)
		ufw allow in on ${IFACE} proto ${PROTO} from $Current_IP to any port ${PORT}
		;;
	esac

	echo ${Current_IP} > $LOGFILE
	echo "Firewall has been updated"
else
	Old_IP=$(cat $LOGFILE)

	if [ "$Current_IP" = "$Old_IP" ] ; then
		echo "IP address has not changed"
	else
		case ${FIREWALL} in
		*)
		iptables*)
			iptables -D INPUT -i ${IFACE} -p ${PROTO} -s ${Old_IP} --dport ${PORT} -j ACCEPT
			iptables -I INPUT -i ${IFACE} -p ${PROTO} -s ${Current_IP} --dport ${PORT} -j ACCEPT
			;;

		ufw*)
			ufw delete allow in on ${IFACE} proto ${PROTO} from ${Old_IP} to any port ${PORT}
			ufw allow in on ${IFACE} proto ${PROTO} from ${Current_IP} to any port ${PORT}
			;;
		esac

		echo $Current_IP > $LOGFILE
		echo "Firewall has been updated"
	fi
fi
