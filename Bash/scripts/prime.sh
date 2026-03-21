#!/usr/bin/env bash
echo -n "Enter Any Number: "
read -r n
i=1; c=1
while [ $i -le "$n" ]; do
	i=$((i + 1))
	r=$((n % i))
	[ $r -eq 0 ] && c=$((c + 1))
done

if [ $c -eq 2 ]; then
	echo "Prime"
else
	echo "Not Prime"
fi
