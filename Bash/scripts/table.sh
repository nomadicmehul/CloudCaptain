#!/usr/bin/env bash
echo -n "Enter The Number upto which you want to Print Table: "
read -r n
i=1
while [ $i -ne 10 ]; do
	i=$((i + 1))
	table=$((i * n))
	echo "$table"
done
