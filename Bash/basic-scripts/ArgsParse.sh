#!/bin/bash

while [ $# -gt 0 ]; do
    case "$1" in
        -h | --help)
            echo -e "${yellow}OpenVPN automatic server and client certificate(s) setup script, v0.01 :: Author: niemal"
            echo -e "      ${white}For a client certificate/package only refer to the create_client.sh script.\n"
            echo -e "Parameters:"
            echo -e "  $lightred--clients $white[integer]$nocolour            - Specifies the amount of client certificates to be automatically created. Default is 1."
            echo -e "  $lightred--servername $white[string(text)]$nocolour    - Defines the server's name. Default is 'server'."
            echo -e "  $lightred--sslconf $white[absolute(path)]$nocolour     - Path for the openssl.cnf creation. It is created by default at '/etc/openvpn/openssl.cnf'."
            echo -e "  $lightred--certs $white[absolute(path)]$nocolour       - Path to the certificates directory. If it doesn't exist, it gets created. Default is '/etc/openvpn/certs'."
            echo -e "  $lightred--certmodulus $white[integer(bit)]$nocolour   - The RSA modulus bit setting. Default is 2048."
            echo -e "  $lightred--expires $white[integer(days)]$nocolour      - The certificate expiration in days. Default is 31337."
            echo -e "  $lightred--duplicate-cn$nocolour                 - Allow duplicate certificates in the network. Default is to not."
            echo -e "  $lightred--cipher $white[string(cipher)]$nocolour      - The server's encryption cipher. Default is AES-256-CBC."
            echo -e "  $lightred--port $white[integer(port)]$nocolour         - The server's port. Default is 1194."
            echo -e "  $lightred--vpnsubnet $white[string(subnet)]$nocolour   - The network's subnet, CIDR 24. Default is '10.8.0.0'."
            echo -e "  $lightred--dns1 $white[string(ip)]$nocolour            - Defines DNS #1 for the server.conf. Default is OpenDNS, 208.67.222.222."
            echo -e "  $lightred--dns2 $white[string(ip)]$nocolour            - Defines DNS #2 for server.conf. Default is OpenDNS, 208.67.220.220."
            echo -e "  $lightred--exitnode$nocolour                     - Configures iptables so the client can access the internet through the VPN. Requires --iface."
            echo -e "  $lightred--iface $white[string(interface)]$nocolour    - Declares the interface for --exitnode. Default is eth0."
            exit 0;;
        -c | --clients)
            shift
            clients=$1;;
        -s | --servername)
            shift
            servername=$1;;
        -ssl | --sslconf)
            shift
            sslconf=$1;;
        -ce | --certs)
            shift
            certs=$1;;
        -cm | --certmodulus)
            shift
            certmodulus=$1;;
        -e | --expires)
            shift
            expiration=$1;;
        -dcn | --duplicate-cn)
            shift
            duplicatecn="duplicate-cn";;
        -ci | --cipher)
            shift
            cipher=$1;;
        -p | --port)
            shift
            port=$1;;
        -pro | --proto)
            shift
            proto=$1;;
        -sub | --vpnsubnet)
            shift
            vpnsubnet=$1;;
        -d1 | --dns1)
            shift
            dns1=$1;;
        -d2 | --dns2)
            shift
            dns2=$1;;
        -en | --exitnode)
            shift
            exitnode=true;;
        -if | --iface)
            shift
            iface=$1;;
    esac
    shift
done
