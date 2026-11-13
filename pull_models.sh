#!/bin/zsh
# Pull every model in models.txt that is not already present locally.
cd "$(dirname "$0")"
while read -r m; do
  [[ -z "$m" ]] && continue
  if ollama list | awk 'NR>1{print $1}' | grep -qx "$m"; then
    echo "[$(date +%T)] already present: $m"
  else
    echo "[$(date +%T)] pulling: $m"
    ollama pull "$m" 2>&1 | tr '\r' '\n' | grep -vE '^\s*$' | tail -2
    echo "[$(date +%T)] done: $m (exit ${pipestatus[1]})"
  fi
done < models.txt
echo "[$(date +%T)] ALL PULLS FINISHED"
