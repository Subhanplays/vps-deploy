# VPS Bot v2

A modern, white-label Discord VPS management bot. Users deploy real LXD
system containers with honest resource limits, manage them from interactive
dashboards, and receive SSH access directly in their DMs.

Everything users see — brand name, colors, status, embed copy, resource
limits, plans, even the auto-generated VPS names — is configurable at runtime
without touching a single line of Python.

```
/create      deploy a new VPS (choose OS, plan or custom resources)
/vps         interactive dashboard: manage, stats, info, SSH, logs, delete
/list        list your VPS instances
/ssh         generate a fresh SSH session (DM only)
/help        interactive help center
/about       about this service
/ping        latency
/admin       hosting control panel (dashboard, ban, kill, stats, ...)
/settings    runtime bot configuration (admins only)
```

## Features

- ✅ White-label branding — name, footer, watermark, socials, colors, avatars
- ✅ Custom embed system — consistent design across every message
- ✅ Dynamic presence rotation (`Watching 13 VPS`, `Managing 4 Servers`, ...)
- ✅ Interactive dashboards with buttons + select menus + confirmation dialogs
- ✅ Automatic VPS name generation (configurable prefixes/separators)
- ✅ tmate/SSH provisioning — credentials delivered **only via DM**
- ✅ Honest resource allocation — validated against real host capacity
- ✅ Server-side permission checks, input sanitization, no command injection
- ✅ Parameterized SQLite, schema migrations, audit logs, Discord log channel

## Requirements

- Python 3.10+ on a machine with the LXD CLI (`lxc`) available
- A Discord bot token with the `applications.commands` scope

## Quick start

```bash
# 1. Install dependencies (on Debian/Ubuntu you may need python3-venv & python3-pip)
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 2. Configure secrets (never commit this file)
cp .env.example .env
#   -> set DISCORD_TOKEN
#   -> optionally ADMIN_USER_IDS / ADMIN_ROLE_IDS

# 3. Customize branding, limits, plans, status (or use /settings later)
#    edit: bot/config/config.json

# 4. Run
python bot/main.py
```

The bot migrates its SQLite database automatically on first run
(`data/vps.db` by default).

## Configuration

### `bot/config/config.json`

The single source of truth for defaults. Highlights:

```jsonc
{
  "branding": {           // white-label identity
    "name": "YourBrand",
    "footer": "Powered by YourBrand",
    "website": "", "support_server": "", "docs_url": "", ...
  },
  "bot": { "status_type": "watching", "status": "your VPS", "online_status": "online" },
  "appearance": {         // embed colors + assets
    "primary_color": "#5865F2", "success_color": "#57F287", ...
  },
  "text": {               // every user-facing embed string ("vps_created_title", ...)
    "vps_created_title": "VPS Created"
  },
  "resources": {          // enforced allocation limits
    "max_ram": "8GB", "max_cpu": 4, "max_disk": "100GB",
    "max_vps_per_user": 2, "global_vps_limit": 50,
    "host_headroom_percent": 10
  },
  "plans": {              // resource plans; toggle .enabled from /settings
    "free":  { "name": "Free",  "ram": "2GB",  "cpu": 1, "disk": "10GB",  "enabled": true },
    "basic": { "name": "Basic", "ram": "4GB",  "cpu": 2, "disk": "25GB",  "enabled": true },
    "pro":   { "name": "Pro",   "ram": "8GB",  "cpu": 4, "disk": "50GB",  "enabled": true }
  },
  "images": {             // OS images from the LXD remotes
    "ubuntu-22.04": { "name": "Ubuntu 22.04", "image": "ubuntu:22.04", "os": "ubuntu" },
    "ubuntu-24.04": { "name": "Ubuntu 24.04", "image": "ubuntu:24.04", "os": "ubuntu" },
    "debian-12":    { "name": "Debian 12",    "image": "images:debian/12", "os": "debian" }
  },
  "lxd": {
    "container_prefix": "vps",
    "storage_pool": "default",
    "profiles": ["default"],
    "autostart": true,
    "storage_quota": true,
    "security_privileged": false,
    "ready_wait": 5
  },
  "name_generator": {
    "enabled": true, "prefixes": ["nova", "atlas", "orbit", "cloud", "zenith"],
    "separator": "-", "random_digits": 4
  },
  "access": { "admin_ids": [], "admin_roles": [], "log_channel": 0 }
}
```

### `.env` — secrets only

```env
DISCORD_TOKEN=your_token
ADMIN_USER_IDS=1234567890        # merged with access.admin_ids
DATABASE_FILE=data/vps.db
LOG_LEVEL=INFO
LOG_FILE=logs/bot.log
```

### Runtime via `/settings`

Every group in `config.json` (General, Appearance, VPS, LXD, Access,
Security, Status Rotation, Plans) can be edited live with `/settings`
(admin only). Changes are stored in the `settings` table and override the
config file immediately.

## Resource honesty

- RAM is enforced with LXD `limits.memory`
- CPU is enforced with LXD `limits.cpu`
- Disk is enforced with a root device `size=` quota **when the storage pool
  driver supports quotas** (zfs/btrfs/lvm). Otherwise creation still works
  but a notice is shown that the quota is advisory — the bot never advertises
  a disk limit that isn't real.
- Requested allocations are checked against actual host CPU/RAM/disk measured
  on the physical host, minus a configurable headroom, *before* anything is
  created.

## Security

- Secrets (token) live only in `.env`
- Input is sanitized (`^[a-z0-9][a-z0-9-]{0,62}$` for names/hostnames)
- `lxc` runs as argument lists via `asyncio.create_subprocess_exec` — never
  `shell=True`, never user-built shell strings
- All SQL is parameterized
- Every VPS lookup is scoped to the calling user; container IDs are never
  accepted from users for discovery
- Destructive actions (`delete`, `reinstall`, `kill-all`) require confirmation
- Admin permissions checked server-side (`access.admin_ids` / `admin_roles`)
- SSH sessions are stored and only ever sent as DMs

## Project layout

```
bot/
├── main.py              entry point, cog loading, error handling
├── app.py               dependency container
├── config/              config.json + Settings (white-label + DB overrides)
├── database/            SQLite, migrations, models/repositories
├── lxd/                 lxc CLI, instance spec, stats/host resources
├── vps/                 business logic: create/manage/ss/status sync
│   ├── manager.py       VPS lifecycle + name generator
│   ├── resources.py     parsing + host/config validation + plans
│   └── ssh.py           tmate install + session capture
├── ui/                  embeds, buttons, selects, modals, renderer
├── services/            status rotation, audit logging, cleanup, value parser
└── commands/            user.py, vps.py, admin.py, settings.py
```

## Deployment notes

- Grant the bot `Send Messages`, `Embed Links`, `Use Slash Commands`.
- Install LXD on the host (`snap install lxd && lxd init`) and create a
  storage pool with a quota-capable driver (`zfs` or `btrfs`) for enforced
  disk limits. A plain `dir` pool still works — disk becomes advisory.
- Run the bot on the LXD host (or as a user in the `lxd` group) so it can
  drive the `lxc` CLI.
- tmate provisioning inside instances requires the instance to reach the
  internet (`apt-get update` / tmate servers).
- Timescales: image download + package install can take 1–3 minutes on first
  create; tune `ssh.*` timeouts and `lxd.ready_wait` in config.

## License

Free to use and rebrand — that's the point.