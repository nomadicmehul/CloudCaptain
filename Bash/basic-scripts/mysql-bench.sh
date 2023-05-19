#!/bin/bash

for run in 1 2 3 ;do
for thread in 1 4 8 16 32 ;do

echo "Performing test RW-${thread}T-${run}"
sysbench --test=fileio --file-total-size=4G --file-test-mode=rndwr --max-time=60 --max-requests=0 --file-block-size=4K --file-num=64 --num-threads=${thread} run > /root/RW-${thread}T-${run}

echo "Performing test RR-${thread}T-${run}"
sysbench --test=fileio --file-total-size=4G --file-test-mode=rndrd --max-time=60 --max-requests=0 --file-block-size=4K --file-num=64 --num-threads=${thread} run > /root/RR-${thread}T-${run}

echo "Performing test SQ-${thread}T-${run}" 
sysbench --test=/usr/share/doc/sysbench/tests/db/oltp.lua --db-driver=mysql --oltp-table-size=1000000 --mysql-db=sysbench --mysql-user=sysbench --mysql-password=password --max-time=60 --max-requests=0 --num-threads=${thread} run > /root/SQ-${thread}T-${run}

done
done
