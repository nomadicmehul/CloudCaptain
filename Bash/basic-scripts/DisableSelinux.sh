# !/bin/bash
#
# Credit goes to this article for the instructions in this shell script:
#   http://blog.zwiegnet.com/linux-server/disable-selinux-centos-7/
#
# Script by: Michael Dichirico (https://github.com/mdichirico/public-shell-scripts)
#
# PLEASE READ:
#
# This script will completely disable SELinux.
#
# In order for the changes to take effect, you'll need to reboot your CentOS 7 server
# afterwards.
#
# INSTRUCTIONS:
# 1. Copy this shell script to your home directory
# 2. Make it executable by using the following command:
#    chmod a+x disable-selinux-on-cent-os-7.sh
# 3. Execute the script with the following command:
#    sudo ./disable-selinux-on-cent-os-7.sh
# 4. After the script finishes, reboot your server.
#
#

sudo sed -i 's/enforcing/disabled/g' /etc/selinux/config /etc/selinux/config
sudo sestatus

echo ""
echo "Finished with script execution!"
echo "In the above output, you'll see that the value of 'SELinux status' is 'enabled'."
echo "That is normal. Do the following two steps:"
echo " 1. reboot your environment: "
echo ""
echo "      sudo shutdown -r now"
echo ""
echo " 2. When you server comes back online, run this command:"
echo ""
echo "      sudo sestatus"
echo ""
echo "    You should then see 'SELinux status: disabled' to confirm that SELinux is in fact disabled"
echo ""