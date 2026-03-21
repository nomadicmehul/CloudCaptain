#!/bin/bash

convert "$1" -trim \( +clone -background grey45 -shadow 80x40+5+10 \) +swap -background transparent -layers merge +repage "$1-s.png"
