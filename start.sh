#!/usr/bin/env bash
#
# Aether Cloud - one-shot host setup + launch.
#
# Installs LXD (no snap required), bridges internet access for containers,
# fixes the storage pool name, builds the Python env, fills in .env and
# finally starts the bot. Safe to re-run; everything is idempotent.
#
# Usage:
#   ./start.sh          # setup everything, then run the bot
#   ./start.sh --check  # setup + verify container networking, then run
#   ./start.sh --setup-only
#

set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

GIT_URL="https://github.com/Subhanplays/vps-deploy.git"
CHECK=0
SETUP_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK=1 ;;
        --setup-only) SETUP_ONLY=1 ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
command -v python3 >/dev/null 2>&1 || { echo "python3 is required."; exit 1; }

DISTRO_ID="$(. /etc/os-release 2>/dev/null && echo "${ID:-unknown}")"
echo "==> Distro: $DISTRO_ID"

say () { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok   () { printf '\033[1;32m   - %s\033[0m\n' "$*"; }
warn () { printf '\033[1;33m   ! %s\033[0m\n' "$*"; }

# Run LXD commands as root when needed (non-root users must also be in the
# lxd group for the bot to work).
lxcrun () { if [ -n "$SUDO" ]; then $SUDO lxc "$@"; else lxc "$@"; fi; }
lxdrun () { if [ -n "$SUDO" ]; then $SUDO lxd "$@"; else lxd "$@"; fi; }

# ---------------------------------------------------------------------------
# 1) Fetch latest code if this is a fresh clone
# ---------------------------------------------------------------------------
if [ ! -d "bot" ]; then
    say "Cloning the repository"
    git clone "$GIT_URL" .
fi
if git rev-parse --git-dir >/dev/null 2>&1 && git remote -v | grep -q origin; then
    git pull --ff-only >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
# 2) Package prerequisites
# ---------------------------------------------------------------------------
if ! command -v git >/dev/null 2>&1 || ! python3 -c "import venv" >/dev/null 2>&1 \
   || ! python3 -m pip --version >/dev/null 2>&1; then
    say "Installing git / python3-venv / python3-pip"
    $SUDO apt-get update -qq
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq \
        git python3-venv python3-pip
fi

# ---------------------------------------------------------------------------
# 3) LXD (snap-first, Zabbly .deb fallback, Debian apt)
# ---------------------------------------------------------------------------
if ! command -v lxc >/dev/null 2>&1; then
    say "Installing LXD"
    if command -v snap >/dev/null 2>&1 && [ "$DISTRO_ID" = "ubuntu" ]; then
        $SUDO snap install lxd
    elif [ "$DISTRO_ID" = "ubuntu" ]; then
        DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq software-properties-common
        $SUDO add-apt-repository -y ppa:zabbly/lxd
        DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq lxd
    elif [ "$DISTRO_ID" = "debian" ]; then
        DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq lxd
    else
        echo "Unsupported distro - install LXD manually, then re-run."
        exit 1
    fi
fi
say "Starting LXD daemon"
$SUDO systemctl enable --now lxd >/dev/null 2>&1 || true
lxdrun waitready --timeout 60 >/dev/null 2>&1 || true
lxdrun init --auto >/dev/null 2>&1 || true

# Promote non-root users into the lxd group (no-op for root)
if [ "$SUDO" != "" ] && ! id -nG | tr ' ' '\n' | grep -qx lxd; then
    $SUDO usermod -aG lxd "$USER"
    warn "added you to the lxd group - re-login before using 'lxc' directly"
fi

# ---------------------------------------------------------------------------
# 4) Storage pool: make sure one named "default" exists
# ---------------------------------------------------------------------------
say "Preparing storage pool"
POOLS="$(lxcrun storage list --format csv | cut -d, -f1 | grep -v '^$' | grep -vix 'NAME' || true)"
if echo "$POOLS" | grep -qx 'default'; then
    ok "pool 'default' already exists"
elif [ -n "$POOLS" ]; then
    FIRST="$(echo "$POOLS" | head -n1)"
    lxcrun storage rename "$FIRST" default
    ok "renamed existing pool '$FIRST' -> default"
else
    if command -v zfs >/dev/null 2>&1; then
        lxcrun storage create default zfs
        ok "created zfs pool 'default' (enforced disk quotas)"
    else
        lxcrun storage create default dir
        warn "created dir pool 'default' - disk quotas are advisory (no zfs/btrfs)"
    fi
fi

DRIVER="$(lxcrun storage show default | awk '/^driver:/{print $2}')"
ok "storage driver: ${DRIVER:-?}"

# ---------------------------------------------------------------------------
# 5) Bridge network + NAT so containers can reach the internet
# ---------------------------------------------------------------------------
say "Preparing LXD bridge + NAT"
if ! lxcrun network list --format csv | cut -d, -f1 | grep -vix 'NAME' | grep -qx 'lxdbr0'; then
    lxcrun network create lxdbr0 ipv4.address=10.10.0.1/24 \
        ipv4.nat=true ipv6.address=none ipv6.nat=false
    ok "created bridge lxdbr0 (10.10.0.0/24, NAT)"
else
    ok "bridge lxdbr0 already exists"
fi
if lxcrun profile show default | grep -q 'eth0'; then
    lxcrun profile device remove default eth0
fi
lxcrun profile device add default eth0 nic network=lxdbr0 name=eth0
ok "default profile now attaches eth0 to lxdbr0"

# ---------------------------------------------------------------------------
# 6) Python environment
# ---------------------------------------------------------------------------
say "Building Python environment"
if [ ! -x ".venv/bin/python" ]; then
    python3 -m venv .venv
fi
"./.venv/bin/python" -m pip install --quiet --upgrade pip
"./.venv/bin/python" -m pip install --quiet -r requirements.txt
ok "dependencies installed"

# ---------------------------------------------------------------------------
# 7) .env
# ---------------------------------------------------------------------------
if [ ! -f ".env" ]; then
    cp .env.example .env
    ok "created .env from .env.example"
fi
if ! grep -q '^DISCORD_TOKEN=.\+' .env 2>/dev/null; then
    read -r -p "   Paste your DISCORD_TOKEN: " TOKEN
    if [ -n "$TOKEN" ]; then
        sed -i "s|^DISCORD_TOKEN=.*|DISCORD_TOKEN=$TOKEN|" .env
        ok "DISCORD_TOKEN saved to .env"
    else
        warn "no token given - set DISCORD_TOKEN in .env before the bot can log in"
    fi
fi

# ---------------------------------------------------------------------------
# 8) Optional: verify container networking end to end
# ---------------------------------------------------------------------------
if [ "$CHECK" = "1" ]; then
    say "Verifying container networking"
    lxcrun delete diag --force >/dev/null 2>&1 || true
    lxcrun launch ubuntu:22.04 diag >/dev/null
    sleep 12
    if lxcrun exec diag -- ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1; then
        ok "containers can reach the internet"
    else
        warn "containers cannot ping 8.8.8.8 - check NAT / ip_forward"
    fi
    lxcrun delete diag --force >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
# 9) Run
# ---------------------------------------------------------------------------
say "Setup complete"
if [ "$SETUP_ONLY" = "1" ]; then
    ok "run the bot any time with: ./.venv/bin/python bot/main.py"
    exit 0
fi
exec ./.venv/bin/python bot/main.py