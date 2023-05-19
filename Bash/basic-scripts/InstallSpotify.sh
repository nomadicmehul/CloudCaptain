#! /bin/bash

#Check root
if [ $(id -u) != "0" ];
then
	echo "Needs to be run by a user with root privilege."
	exit 1
fi

# Check debian packages manager
if [ -n "$(command -v apt-get | wc -l)" != "1" ]
then
	echo "Please use Debian based system"
	exit 1
fi

# Check to see if Spotify repository
echo "  Checking /etc/apt/sources.list for repository."
ssource=`grep -o -E "deb http://repository.spotify.com stable non-free" /etc/apt/sources.list | wc -l`
if [ $ssource -eq 0 ]; then
	echo '' | sudo tee -a /etc/apt/sources.list.d/spotify.list
	echo '## SPOTIFY-CLIENT' | sudo tee -a /etc/apt/sources.list.d/spotify.list
	echo 'deb http://repository.spotify.com stable non-free' | sudo tee -a /etc/apt/sources.list.d/spotify.list
else
	echo "  Skipping addition to /etc/apt/sources.list.d/sources.list."
fi

# Verify downloaded packages
sudo apt-key adv --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys BBEBDCB318AD50EC6865090613B00F1FD2C19886

# Run apt-get update
sudo apt-get update

# Install spotify!
sudo apt-get install spotify-client

echo "  Done. Add Spotify to your system"
echo "  Press any key to continue."
read
