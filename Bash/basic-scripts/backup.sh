#!/bin/sh

# Taken from http://samba.anu.edu.au/rsync/examples.html

# This script does personal backups.  You will end up with a 7 day 
# rotating incremental backup.  The incrementals will go into 
# subdirectories named after the day of the week, and the
# current full backup goes into a directory called "current".

# directory to backup
BDIR=$HOME

# excludes file - this contains a wildcard pattern per line of files
# to exclude
EXCLUDES=$HOME/local/etc/backup_exclude

# the backup location
BLOCATION=/Volumes/Storage/Backups

# set to the location of directory containing additional
# backup scripts to be run prior to backing up
SCRIPTS_DIR=$HOME/local/etc/backup.d

#######################################################

# run the backup scripts located in SCRIPTS_DIR
if [[ -d $SCRIPTS_DIR ]]
then
   for file in `ls $SCRIPTS_DIR`
   do
      $SCRIPTS_DIR/$file $BDIR 2> /dev/null > /dev/null
      if (( $? != 0 )) 
      then
         ERRCODE=$?
         echo execution of script $SCRIPTS_DIR/$file failed
         exit $?
      fi
   done
fi

# set some options for rsync
BACKUPDIR=`date +%A`
OPTS="--extended-attributes --force --ignore-errors --delete-excluded --exclude-from=$EXCLUDES
      --delete --times --backup --backup-dir=../$BACKUPDIR -a"

# the following line clears the last weeks incremental directory
[ -d $HOME/emptydir ] || mkdir $HOME/emptydir
rsync --delete -a $HOME/emptydir/ $BLOCATION/$USER/$BACKUPDIR/
rmdir $HOME/emptydir

# now the actual transfer
rsync $OPTS $BDIR $BLOCATION/$USER/current
