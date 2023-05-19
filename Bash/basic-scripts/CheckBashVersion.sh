#!/bin/bash

echo "Checking bash version."
version=$(bash --version | grep 'GNU bash' | sed 's/.*version \([0-9]*\)\..*/\1/')
if [ "$version" = "4" ]; then
    echo "Bash version compatible."
  else
    echo "Bash version incompatible. Must be at least 4, yours is $version. You can enter this command in the terminal: apt-get install --only-upgrade bash"
    exit 1
fi
