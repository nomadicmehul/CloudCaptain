#!/bin/bash

for file in $(find -iname '*.CHK'); do C_EXT=$(file --mime-type $file | cut -d' ' -f2 | xargs -I {} grep {} /etc/mime.types | awk '{ print $2; }'); if [ -n "$C_EXT" ]; then rename -v "s/.CHK$/.$C_EXT/i" $file; fi; done
