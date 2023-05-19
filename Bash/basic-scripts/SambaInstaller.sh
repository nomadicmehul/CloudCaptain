# !/bin/bash
# Samba Installer

if [ "$EUID" -ne 0 ]
  then echo "Please this app run as root. 'sudo $0'"
  exit
fi

apt install samba -y

hostname=`hostname`

cat > /etc/samba/smb.conf <<EOF
[global]
    workgroup = WORKGROUP
    #usershare allow guests = yes security=share security=user
    security = user
    guest account = nobody
    follow symlinks = yes
    wide links = no
    unix extensions = no
    lock directory = /var/cache/samba
    netbios name = $hostname
    follow symlinks = yes
    wide links = yes
    unix extensions = no
    log file = /dev/null
bind interfaces only = yes
#interfaces = eth0
#interfaces = 212.154.12.48 10.0.0.5
#encrypt passwords = no
#obey pam restrictions = yes
#pam password change = yes
#client plaintext auth = yes
#client ntlmv2 auth = no

[Root]
        comment = Root
        path = /
        read only = No
        #access based share enum = Yes
        browsable = yes
        valid user = root
EOF

pass=`< /dev/urandom tr -dc a-z | head -c${1:-5}`

echo "$pass
$pass" | smbpasswd -a root

service smbd restart
service nmbd restart

echo "Username : root
Password: $pass
"
