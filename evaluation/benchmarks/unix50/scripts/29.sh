#!/bin/bash
export IN_PRE=${IN_PRE:-$PASH_TOP/evaluation/benchmarks/unix50/inputs}
IN9_7=$IN_PRE/09.7.txt
# 9.7: Four corners
cat "$IN9_7" | sed 2d | sed 2d | tr -c '[A-Z]' '\n' | tr -d '\n'
