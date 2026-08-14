# White-Label Discord VPS Bot (KVM/QEMU)

A simple, lightweight Discord bot that deploys **real KVM/QEMU virtual machines**
directly on the Linux host where it runs. No external VPS provider API, no fake
containers pretending to be VPSs.

```
Discord -> /vps -> Create VPS -> OS -> RAM -> Storage -> CPU -> Confirm
    -> KVM/QEMU creates a real VM
    -> cloud-init configures it
    -> tmate session generated
    -> private SSH connection sent to the owner
```

## Features

- **Real KVM VPS** - every instance is a libvirt-managed QEMU virtual machine
  (`virsh list --all` shows them).
- **Interactive flow** - `/vps` lets users pick OS, RAM, storage and CPU through
  modals and buttons, then shows a confirmation before deploying.
- **RAM overcommit (overselling)** - with `RAM_OVERCOMMIT=true` the bot budgets
  against `MAX_ALLOCATED_RAM` instead of physical RAM. A host with 8 GB RAM can
  intentionally allocate 120 GB of *virtual* RAM.
- **CPU overcommit** - same concept for vCPUs via `MAX_ALLOCATED_CPU`.
- **Sparse QCOW2 disks** - virtual disk size can be larger than its physical
  footprint, but the bot still checks real free storage before creating a disk.
- **cloud-init** - each VM gets a unique, auto-generated cloud-init seed
  (hostname, user, SSH key, networking, packages, tmate).
- **tmate SSH** - the bot boots the VM, waits for networking, starts tmate and
  sends the private SSH connection only to the VPS owner.
- **Admin panel** - `/admin` with host resources, overcommit state, VPS listing,
  start/stop/restart/delete/reinstall/kill, runtime limit changes.
- **SQLite** - users, VPS instances, deployment jobs, host settings, audit logs.
- **Fully white-label** - branding comes entirely from `.env`.

## Host requirements

This bot must run on a Linux host with KVM support. It does **not** fake KVM.

```bash
ls -l /dev/kvm            # must exist
egrep -c '(vmx|svm)' /proc/cpuinfo   # > 0
virsh list --all          # libvirt working
qemu-system-x86_64 --version
```

Required packages (Debian/Ubuntu):

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv \
    qemu-system-x86 qemu-utils libvirt-daemon-system libvirt-clients \
    cloud-image-utils ssh openssh-client
sudo systemctl enable --now libvirtd
```

If `/dev/kvm` does not exist the bot logs a clear warning and refuses to deploy.

## Installation

```bash
git clone <your-repo> vps-bot
cd vps-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: set DISCORD_TOKEN and your branding
```

The bot creates `/var/lib/vpsbot` (disks, instances, keys, database) on first
run. You can override the paths in `.env`.

### Download the cloud images

```bash
bash download_images.sh
```

or manually place the images at the paths in `.env`:

```bash
mkdir -p /var/lib/vpsbot/images/ubuntu /var/lib/vpsbot/images/debian
# Ubuntu 24.04 and Debian 12 *generic cloud* qcow2 images
# rename to ubuntu.qcow2 / debian.qcow2
```

> Only the *generic cloud* images work with cloud-init.

### Run

```bash
python3 bot.py
```

## Configuration

Everything is configured via `.env` (see `.env.example`). The essential values
a provider changes:

```
BOT_NAME=
BOT_STATUS=
BOT_LOGO=
BOT_COLOR=
BOT_FOOTER=
BOT_WEBSITE=
BOT_SUPPORT_SERVER=
```

Runtime-adjustable limits (changeable with `/admin config`):

| Key                  | Meaning                                   |
|----------------------|-------------------------------------------|
| `creation_enabled`   | allow/disallow VPS creation               |
| `ram_overcommit`     | enable/disable RAM overselling            |
| `cpu_overcommit`     | enable/disable CPU overselling            |
| `max_allocated_ram`  | max budgeted virtual RAM (GB)             |
| `max_allocated_cpu`  | max budgeted vCPUs                        |
| `max_disk_per_vps`   | max virtual disk per VPS (GB)             |
| `max_vps_per_user`   | VPS limit per user                        |

## Overcommit - important

Overcommit means *virtual allocation* is allowed to exceed *physical
resources*. The bot always shows both numbers separately and never claims the
host physically has more than it does:

```
Physical RAM: 8.0 GB
Allocated VPS RAM: 96.0 GB
Maximum Allocation: 120.0 GB
RAM Overcommit: 🟢 Enabled
```

For disk there is **no overcommit**: the bot always checks actual free physical
storage before creating a disk, because sparse QCOW2 files still grow.

## Commands

User:

```
/vps                    Main menu (create / list / manage)
/ping                   Latency
/about                  Bot info
```

Admin (role in `ADMIN_ROLE_ID` or user in `ADMIN_USER_IDS`):

```
/admin panel            Admin overview + buttons
/admin vps list         List all VPS
/admin vps info <id>    VPS details            e.g. /admin vps info VPS-0001
/admin vps start <id>
/admin vps stop <id>
/admin vps restart <id>
/admin vps kill <id>    Force stop
/admin vps delete <id>
/admin vps reinstall <id>
/admin vps resources    Host resources + overcommit
/admin config <key> <value>
```

## Security notes

- Only predefined `virsh`/`qemu-img`/`ssh` commands are executed. User input is
  never passed to a shell and never used to build arbitrary commands.
- VM names are validated against a strict `[a-z0-9-]` pattern; Discord usernames
  are never used as VM names (VPS IDs like `VPS-0001` / `vps-0001` are used).
- Ownership checks: only the owner can start/stop/delete their VPS.
- Admin authorization for every `/admin` command.
- Rate limiting, per-user deployment locks, command timeouts.
- Failed deployments are fully cleaned up (VM, disk, seed, DB record, audit).
- tmate connection strings are only ever sent to the VPS owner in DMs.

## Project layout

```
vps-bot/
├── bot.py            Discord bot + UI
├── config.py         configuration loader
├── database.py       SQLite layer
├── vps.py            virsh/KVM operations + resource checks
├── cloud_init.py     per-VPS cloud-init generation
├── tmate.py          SSH + tmate session handling
├── requirements.txt
├── .env.example
├── README.md
├── download_images.sh
└── images/
    ├── ubuntu/
    └── debian/
```
