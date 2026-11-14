#!/bin/zsh
set -eu
cd -- "${0:A:h}"
mkdir -p data/build/cache
if [[ ! -x data/build/media || Media.swift -nt data/build/media ]]; then
  swiftc -module-cache-path "$PWD/data/build/cache" Media.swift -o data/build/media
fi
exec python3 -B server.py "$@"
