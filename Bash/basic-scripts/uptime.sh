#!/bin/bash

uptime | sed -le 's/^.*: \(.*\)$/\1/'
