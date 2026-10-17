#!/usr/bin/env bash
# Download and unpack the BGL and OpenStack datasets (Loghub, Zenodo record 8196385)
# into data/raw/. Skips files that are already present.
set -euo pipefail

RAW="$(cd "$(dirname "$0")/.." && pwd)/data/raw"
mkdir -p "$RAW/openstack"
cd "$RAW"

if [ ! -f BGL.log ]; then
    [ -f BGL.zip ] || curl -fL -o BGL.zip "https://zenodo.org/records/8196385/files/BGL.zip?download=1"
    unzip -oq BGL.zip BGL.log
fi

if [ ! -f openstack/openstack_normal2.log ]; then
    [ -f OpenStack.tar.gz ] || curl -fL -o OpenStack.tar.gz "https://zenodo.org/records/8196385/files/OpenStack.tar.gz?download=1"
    tar xzf OpenStack.tar.gz -C openstack
fi

wc -l BGL.log openstack/*.log
