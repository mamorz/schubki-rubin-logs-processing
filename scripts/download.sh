#!/usr/bin/env bash
# Full Loghub corpora, pinned to manifests/full_dataset_sources_v1.json.
# Existing files are verified and never overwritten; publish only complete files.
set -euo pipefail
RAW="$(cd "$(dirname "$0")/.." && pwd)/data/raw"
for tool in curl unzip tar; do
    command -v "$tool" >/dev/null || { echo "Install $tool first." >&2; exit 1; }
done
if command -v sha256sum >/dev/null; then
    digest() { sha256sum "$1"; }
else
    command -v shasum >/dev/null || { echo "Install sha256sum or shasum first." >&2; exit 1; }
    digest() { shasum -a 256 "$1"; }
fi
verify() {
    local actual
    actual="$(digest "$1")"
    if [ "${actual%% *}" != "$2" ]; then
        echo "Checksum mismatch: $1. Preserving it; inspect it before retrying." >&2
        exit 1
    fi
}
mkdir -p "$RAW/openstack"
STAGE="$(mktemp -d "$RAW/.download.XXXXXXXX")"
trap 'rm -rf -- "$STAGE"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

archive() {
    local name="$1" expected="$2"
    if [ ! -e "$RAW/$name" ]; then
        curl --fail --location --retry 3 --connect-timeout 20 --max-time 1800 \
            --output "$STAGE/$name" "https://zenodo.org/records/8196385/files/$name?download=1"
        verify "$STAGE/$name" "$expected"
        mv -n "$STAGE/$name" "$RAW/$name"
    fi
    verify "$RAW/$name" "$expected"
}

# SHA-256 values match the checked-in immutable source manifest. Each member is
# extracted separately, so an interrupted extraction cannot look complete.
while read -r name expected; do
    if [ -e "$RAW/$name" ]; then
        verify "$RAW/$name" "$expected"
        continue
    fi
    if [ "$name" = BGL.log ]; then
        archive BGL.zip d67fd82a711aea0157a9b83175892c6ee60e384a2ddf5bc51f39118453816da8
        unzip -p "$RAW/BGL.zip" BGL.log > "$STAGE/member"
    else
        archive OpenStack.tar.gz 87c98c5ed03262e05cdb7a6f3717033df76d88fda0f7d2db23bd9fa4200f1879
        tar -xOf "$RAW/OpenStack.tar.gz" "${name#openstack/}" > "$STAGE/member"
    fi
    verify "$STAGE/member" "$expected"
    mv -n "$STAGE/member" "$RAW/$name"
    verify "$RAW/$name" "$expected"
done <<'MEMBERS'
BGL.log 666130b15ef44eb32fd02bd053e6c6e007c37696b5e7e8b9d8e45b729876a5d2
openstack/anomaly_labels.txt 0882aa484f285e3dda476ff39ed739603c446f27b76bb113e284cda6ebb09d0b
openstack/openstack_abnormal.log 7ab718fbb2f2b804893955cecf37ad9533cfa0efaf5b03009474d5bb019b6af4
openstack/openstack_normal1.log 4e4d47347bdae198056bb3b0a8a755e1cb0100d6c6a30bbd4058684234769199
openstack/openstack_normal2.log 3c51741dcea1fb0731d1f86588b12587707e7abea01e725ee16c9441b8b68db7
MEMBERS
echo 'BGL and all OpenStack source files verified.'
