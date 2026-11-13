#!/bin/zsh
# Fake "agent": prints colored output and reports ANY input it receives.
name=$1; i=0
while true; do
  if read -t 0.4 -k 1 ch; then print -r -- "$(date +%H:%M:%S.%N 2>/dev/null || date +%T) GOT-INPUT[$name]: $(printf %q "$ch")" >> "${TABSPACE_INPUT_LOG:-/dev/null}"; print "GOT INPUT"; fi
  i=$((i+1)); printf '\e[3%dm%s working… step %d\e[0m\n' $((i%6+1)) "$name" $i
done
