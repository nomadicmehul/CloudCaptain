#!/bin/bash

ok() {
    echo -e '\e[32m'$1'\e[m';
}

#exp
ok "❯❯❯ apt-get update"

die() {
    echo -e '\e[1;31m'$1'\e[m'; exit 1;
}

die "❯❯❯ apt-get update"
