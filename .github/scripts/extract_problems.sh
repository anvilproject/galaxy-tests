#!/usr/bin/env bash

for d in $(find reports/anvil-$1/tool-tests -type d -name "$2") ; do
	#echo $d
	python .github/scripts/extract_problems.py $d
done
