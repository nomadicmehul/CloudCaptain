#!/bin/bash

IPPFX=$1
for i in `seq 1 255` ; do LIST="$LIST ${IPPFX}.$i" ; done
for i in $LIST ; do
    ENTRY="`host $i`"
    [ $? -ne 0 ] && continue
    ENTRY=`echo "$ENTRY" l sed -e 's/.* //' -e 's/\.$//'`
    echo -e "$i\t$ENTRY"
done
