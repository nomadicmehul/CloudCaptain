#!/bin/bash

sudo -u ${USERNAME} normal_command_1
top -bn5 >> top_logs.txt
*/10 * * * * /path/to/script
FREE_DATA=`free -m | grep Mem`
