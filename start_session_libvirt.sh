#!/usr/bin/env bash
# Start libvirt in per-user "session" mode so real QEMU VMs can run without
# /dev/kvm and without the system libvirtd service. Use this in containers and
# sandboxes (e.g. CodeSandbox), where software emulation (TCG) is used anyway.
#
# Usage:
#   bash start_session_libvirt.sh
#   (then put LIBVIRT_URI=qemu:///session in your .env)

set -e

# The daemons live in /usr/sbin, which is often not on a non-root user's PATH.
VIRTQEMUD="$(command -v virtqemud || true)"
[ -z "$VIRTQEMUD" ] && [ -x /usr/sbin/virtqemud ] && VIRTQEMUD=/usr/sbin/virtqemud
VIRTLOGD="$(command -v virtlogd || true)"
[ -z "$VIRTLOGD" ] && [ -x /usr/sbin/virtlogd ] && VIRTLOGD=/usr/sbin/virtlogd

if [ -z "$VIRTQEMUD" ]; then
    echo "ERROR: virtqemud not found. Install it first:"
    echo "  sudo apt update && sudo apt install -y libvirt-daemon-driver-qemu"
    exit 1
fi

if [ -n "$VIRTLOGD" ]; then
    "$VIRTLOGD" --session >/dev/null 2>&1 || true
fi

"$VIRTQEMUD" --session >/dev/null 2>&1 || true

sleep 1

if ! virsh -c qemu:///session list >/dev/null 2>&1; then
    echo "ERROR: could not connect to qemu:///session."
    echo "Run 'virsh -c qemu:///session list' manually to see the error."
    exit 1
fi

echo "Session libvirt is running."
echo "Now set this in your .env file:  LIBVIRT_URI=qemu:///session"
echo "Verify VMs with:  virsh -c qemu:///session list --all"
