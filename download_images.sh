#!/usr/bin/env bash
#
# Download Ubuntu 24.04 and Debian 12 *generic cloud* images and convert them
# to qcow2 ready for the bot. Requires qemu-img (qemu-utils).
#
# Usage: bash download_images.sh [target_dir]
set -euo pipefail

TARGET="${1:-/var/lib/vpsbot/images}"

UBUNTU_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
UBUNTU_DEST="$TARGET/ubuntu/ubuntu.qcow2"

DEBIAN_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2"
DEBIAN_DEST="$TARGET/debian/debian.qcow2"

mkdir -p "$TARGET/ubuntu" "$TARGET/debian"

fetch_convert() {
    local url="$1" dest="$2"
    local tmp="$dest.tmp"
    if [ -f "$dest" ]; then
        echo "[skip] $dest already exists"
        return
    fi
    echo "[fetch] $url"
    wget -O "$tmp" "$url" || curl -L -o "$tmp" "$url"
    echo "[convert] -> $dest"
    if [[ "$tmp" == *.img ]]; then
        qemu-img convert -O qcow2 "$tmp" "$dest"
        rm -f "$tmp"
    else
        mv -f "$tmp" "$dest"
    fi
    qemu-img info "$dest" | head -n 8
}

fetch_convert "$UBUNTU_URL" "$UBUNTU_DEST"
fetch_convert "$DEBIAN_URL" "$DEBIAN_DEST"

echo
echo "Images ready:"
echo "  $UBUNTU_DEST"
echo "  $DEBIAN_DEST"
echo
echo "Point UBUNTU_IMAGE / DEBIAN_IMAGE in .env at these files if different."
