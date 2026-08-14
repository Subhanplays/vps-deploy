"""Real KVM/QEMU virtual machine management via libvirt's ``virsh``.

Every operation is a predefined, argument-validated call. The bot NEVER
builds shell commands from user input, never uses ``shell=True`` and never
passes free-form QEMU/virsh arguments supplied by a Discord user.

Supported operations: create, start, stop, restart, shutdown, delete,
reinstall, kill (force stop), status.
"""

import asyncio
import os
import re
import shutil
import subprocess

from config import config
import database as db
import cloud_init

# Only these characters are ever allowed in a VM name.
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class VpsError(Exception):
    """Raised when a virtualization operation fails."""


# --------------------------------------------------------------------------
# Low level helpers
# --------------------------------------------------------------------------
def _run(cmd, timeout=30):
    """Run a predefined command list synchronously. Never shell=True."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise VpsError(f"Command timed out: {' '.join(cmd)}")
    except FileNotFoundError:
        raise VpsError(f"Required executable not found: {cmd[0]}")
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


async def arun(cmd, timeout=60):
    """Run a predefined command list asynchronously."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise VpsError(f"Command timed out: {' '.join(cmd)}")
    return proc.returncode, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()


def _check_name(name):
    if not _SAFE_NAME.match(name):
        raise VpsError("Invalid VM name rejected.")


def _virsh(*args):
    return _run(["virsh", *args], timeout=45)


def _emulator():
    for candidate in ("/usr/bin/kvm", "/usr/bin/qemu-system-x86_64", "/usr/bin/qemu-system-x86_64"):
        if os.path.exists(candidate):
            return candidate
    return "/usr/bin/qemu-system-x86_64"


# --------------------------------------------------------------------------
# Environment checks
# --------------------------------------------------------------------------
def check_environment():
    """Return a list of (ok: bool, message: str) host capability checks."""
    checks = []
    checks.append((os.path.exists("/dev/kvm"), "KVM acceleration (/dev/kvm) available"))
    checks.append((shutil.which("virsh") is not None, "virsh (libvirt) installed"))
    checks.append((shutil.which("qemu-img") is not None, "qemu-img installed"))
    checks.append((shutil.which("cloud-localds") is not None, "cloud-localds (cloud-image-utils) installed"))
    checks.append((shutil.which("ssh-keygen") is not None, "ssh-keygen available"))
    checks.append((shutil.which("ssh") is not None, "ssh client available"))
    for key in ("ubuntu", "debian"):
        image = config.image_map.get(key)
        checks.append((image and os.path.exists(image), f"{key} base image exists ({image})"))
    return checks


def host_kvm_available():
    return os.path.exists("/dev/kvm")


def physical_resources():
    """Physical host resources. Never claim overcommitted values here."""
    cpus = 0
    try:
        with open("/proc/cpuinfo") as fh:
            seen = set()
            phys = core = None
            for line in fh:
                line = line.strip()
                if line.startswith("physical id"):
                    phys = line.split(":")[1].strip()
                elif line.startswith("core id"):
                    core = line.split(":")[1].strip()
                elif line == "":
                    if phys is not None and core is not None:
                        seen.add((phys, core))
                    phys = core = None
            cpus = len(seen)
    except OSError:
        pass
    if not cpus:
        cpus = os.cpu_count() or 1

    mem_kb = 0
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    mem_kb = int(line.split()[1])
                    break
    except (OSError, ValueError, IndexError):
        pass
    ram_gb = round(mem_kb / (1024 * 1024), 1)

    usage = None
    try:
        os.makedirs(config.storage_dir, exist_ok=True)
        usage = shutil.disk_usage(config.storage_dir)
    except OSError:
        try:
            usage = shutil.disk_usage(os.getcwd())
        except OSError:
            usage = shutil.disk_usage("/")
    storage_gb = round(usage.total / (1024 ** 3), 1)
    free_gb = round(usage.free / (1024 ** 3), 1)

    return {
        "cpus": cpus,
        "ram_gb": ram_gb,
        "storage_gb": storage_gb,
        "free_gb": free_gb,
        "kvm": host_kvm_available(),
    }


def allocation_state():
    """Combine physical resources with virtual allocation + limits."""
    phys = physical_resources()
    return {
        "physical_cpu": phys["cpus"],
        "physical_ram_gb": phys["ram_gb"],
        "physical_storage_gb": phys["storage_gb"],
        "free_storage_gb": phys["free_gb"],
        "allocated_cpu": db.allocated_cpu(),
        "allocated_ram_gb": round(db.allocated_ram(), 1),
        "allocated_disk_gb": db.allocated_disk(),
        "max_ram_gb": db.settings_float("max_allocated_ram", config.max_allocated_ram),
        "max_cpu": db.settings_int("max_allocated_cpu", config.max_allocated_cpu),
        "ram_overcommit": db.settings_bool("ram_overcommit", config.ram_overcommit),
        "cpu_overcommit": db.settings_bool("cpu_overcommit", config.cpu_overcommit),
        "max_vps_per_user": db.settings_int("max_vps_per_user", config.max_vps_per_user),
        "max_disk_per_vps": db.settings_float("max_disk_per_vps", config.max_disk_per_vps),
        "creation_enabled": db.settings_bool("creation_enabled", True),
        "kvm": phys["kvm"],
    }


# --------------------------------------------------------------------------
# Resource validation
# --------------------------------------------------------------------------
def check_spec(os_name, cpu, ram, disk):
    """Validate a requested spec against configured limits and the real host.

    RAM/CPU honour the overcommit settings (allocation budget instead of the
    physical numbers). Disk is ALWAYS checked against actual free physical
    storage because sparse QCOW2 files still grow.
    """
    state = allocation_state()

    if os_name not in config.image_map:
        return False, "Unsupported operating system."
    if not state["kvm"]:
        return False, "KVM acceleration is unavailable on this host."
    if not state["creation_enabled"]:
        return False, "VPS creation is currently disabled by an administrator."
    if not 1 <= cpu <= config.max_cpu_per_vps:
        return False, f"CPU must be between 1 and {config.max_cpu_per_vps} cores."
    if not 1 <= ram <= int(config.max_ram_per_vps):
        return False, f"RAM must be between 1 and {int(config.max_ram_per_vps)} GB."
    if not 1 <= disk <= int(state["max_disk_per_vps"]):
        return False, f"Disk must be between 1 and {int(state['max_disk_per_vps'])} GB."

    # RAM allocation budget
    new_ram = state["allocated_ram_gb"] + ram
    if state["ram_overcommit"]:
        if new_ram > state["max_ram_gb"]:
            return False, (
                f"RAM allocation limit reached: {new_ram:.1f} GB would exceed the "
                f"configured maximum of {state['max_ram_gb']:.1f} GB."
            )
    else:
        if new_ram > state["physical_ram_gb"]:
            return False, (
                f"Requested RAM ({ram} GB) would exceed the physical host RAM "
                f"({state['physical_ram_gb']:.1f} GB)."
            )

    # CPU allocation budget
    new_cpu = state["allocated_cpu"] + cpu
    if state["cpu_overcommit"]:
        if new_cpu > state["max_cpu"]:
            return False, (
                f"CPU allocation limit reached: {new_cpu} vCPUs would exceed the "
                f"configured maximum of {state['max_cpu']}."
            )
    else:
        if new_cpu > state["physical_cpu"]:
            return False, (
                f"Requested CPU ({cpu}) would exceed the physical host cores "
                f"({state['physical_cpu']})."
            )

    # Disk always checks real free space (sparse overcommit is not reported as free).
    if disk > state["free_storage_gb"]:
        return False, (
            f"Not enough physical storage: {disk} GB requested but only "
            f"{state['free_storage_gb']:.1f} GB free on the host."
        )
    return True, "ok"


# --------------------------------------------------------------------------
# VM lifecycle
# --------------------------------------------------------------------------
def status_of(vm_name):
    _check_name(vm_name)
    code, out, err = _virsh("domstate", vm_name)
    if code != 0:
        return "stopped"
    state = out.strip().lower()
    if "running" in state:
        return "running"
    if "shut off" in state or "shutdown" in state:
        return "stopped"
    if "paused" in state:
        return "paused"
    if "error" in state.lower():
        return "error"
    return state or "stopped"


async def create_vm(vps, pubkey, progress=None):
    """Create the disk + seed, define the domain and start the VM.

    ``vps`` is a database row dict. ``progress`` is an optional async
    callback receiving step strings. Raises VpsError on failure so the
    caller can run cleanup.
    """
    vm_name = vps["vm_name"]
    _check_name(vm_name)
    ram_kib = int(vps["ram"]) * 1024 * 1024
    disk_gb = vps["disk"]
    base_image = config.image_map.get(vps["os"])
    if not base_image or not os.path.exists(base_image):
        raise VpsError(f"Base image for {vps['os']} is missing on the host.")

    os.makedirs(config.storage_dir, exist_ok=True)
    os.makedirs(config.instances_dir, exist_ok=True)
    disk_path = os.path.join(config.storage_dir, f"{vm_name}.qcow2")
    instance_dir = os.path.join(config.instances_dir, vm_name)

    if progress:
        await progress("✅ Allocating resources")

    # Create the sparse QCOW2 overlay. Its *virtual* size is the requested
    # disk size; the physical file only grows as data is written.
    code, out, err = _run(
        [
            "qemu-img", "create", "-f", "qcow2", "-F", "qcow2",
            "-b", base_image, disk_path, f"{disk_gb}G",
        ],
        timeout=120,
    )
    if code != 0:
        raise VpsError(f"Failed to create virtual disk: {err}")

    if progress:
        await progress("✅ Creating virtual disk")

    try:
        seed_path = cloud_init.write_seed(instance_dir, vm_name, pubkey)
    except RuntimeError as exc:
        raise VpsError(str(exc))

    if progress:
        await progress("🔄 Configuring cloud-init")

    # Re-allocate from the same pool each run so nothing is lost after an
    # interrupted deployment. The DB record already holds vm_uuid; re-read it
    # fresh so create + define always agree.
    vm_uuid = vps["vm_uuid"]

    xml = _domain_xml(vm_name, vm_uuid, ram_kib, vps["cpu"], disk_path, seed_path)

    xml_path = os.path.join(instance_dir, "domain.xml")
    with open(xml_path, "w") as fh:
        fh.write(xml)

    code, out, err = _run(["virsh", "define", xml_path], timeout=30)
    if code != 0:
        raise VpsError(f"Failed to define KVM virtual machine: {err}")

    if progress:
        await progress("✅ Creating KVM virtual machine")

    code, out, err = _virsh("start", vm_name)
    if code != 0:
        raise VpsError(f"Failed to start the virtual machine: {err}")

    if progress:
        await progress("✅ Starting VPS")
    return disk_path, seed_path, instance_dir


async def wait_for_ip(vm_name, timeout=180, progress=None):
    """Poll libvirt DHCP leases / ARP until the VM has an IPv4 address."""
    _check_name(vm_name)
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        mac = _vm_mac(vm_name)
        if mac:
            for source in ("lease", "arp"):
                code, out, err = _virsh("domifaddr", vm_name, "--source", source)
                if code == 0:
                    for line in out.splitlines():
                        match = re.search(r"(\d+\.\d+\.\d+\.\d+)/", line)
                        if match and mac.lower() in line.lower():
                            if progress:
                                await progress("✅ Configuring network")
                            return match.group(1)
        await asyncio.sleep(5)
    raise VpsError(f"Timed out waiting for an IP address for {vm_name}.")


def _vm_mac(vm_name):
    _check_name(vm_name)
    code, out, err = _virsh("dumpxml", vm_name)
    if code != 0:
        return None
    match = re.search(r"<mac address='([0-9a-fA-F:]+)'", out)
    return match.group(1) if match else None


def _domain_xml(vm_name, vm_uuid, ram_kib, vcpu, disk_path, seed_path):
    if config.bridge_name:
        iface = (
            f"    <interface type='bridge'>\n"
            f"      <source bridge='{config.bridge_name}'/>\n"
            f"      <model type='virtio'/>\n"
            f"    </interface>"
        )
    else:
        iface = (
            f"    <interface type='network'>\n"
            f"      <source network='{config.network_name}'/>\n"
            f"      <model type='virtio'/>\n"
            f"    </interface>"
        )
    return f"""<domain type='kvm'>
  <name>{vm_name}</name>
  <uuid>{vm_uuid}</uuid>
  <memory unit='KiB'>{ram_kib}</memory>
  <currentMemory unit='KiB'>{ram_kib}</currentMemory>
  <vcpu placement='static'>{vcpu}</vcpu>
  <os>
    <type arch='x86_64' machine='pc'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
    <apic/>
  </features>
  <cpu mode='host-passthrough' check='none'/>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>
  <devices>
    <emulator>{_emulator()}</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{seed_path}'/>
      <target dev='sdb' bus='sata'/>
      <readonly/>
    </disk>
{iface}
    <serial type='pty'>
      <target port='0'/>
    </serial>
    <console type='pty'>
      <target type='serial' port='0'/>
    </console>
    <channel type='unix'>
      <source mode='bind'/>
      <target type='virtio' name='org.qemu.guest_agent'/>
    </channel>
    <memballoon model='virtio'/>
    <rng model='virtio'>
      <backend model='random'>/dev/urandom</backend>
    </rng>
  </devices>
</domain>
"""


async def start_vm(vm_name):
    _check_name(vm_name)
    if status_of(vm_name) == "running":
        return True
    code, out, err = _virsh("start", vm_name)
    return code == 0


async def stop_vm(vm_name, timeout=60):
    """Graceful ACPI shutdown, escalating to a forced destroy."""
    _check_name(vm_name)
    code, out, err = _virsh("shutdown", vm_name)
    if code != 0:
        # Not running or already off.
        return status_of(vm_name) != "running"
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        await asyncio.sleep(5)
        if status_of(vm_name) != "running":
            return True
    await kill_vm(vm_name)
    return status_of(vm_name) != "running"


async def kill_vm(vm_name):
    """Force stop (virsh destroy)."""
    _check_name(vm_name)
    code, out, err = _virsh("destroy", vm_name)
    return code == 0


async def restart_vm(vm_name):
    _check_name(vm_name)
    code, out, err = _virsh("reboot", vm_name)
    if code == 0:
        return True
    # Fall back to stop + start.
    await stop_vm(vm_name, timeout=30)
    return await start_vm(vm_name)


def delete_vm(vm_name):
    """Undefine + destroy the domain and remove its files."""
    _check_name(vm_name)
    _virsh("destroy", vm_name)
    _virsh("undefine", vm_name)
    code, out, err = _virsh("domstate", vm_name)
    if code == 0:
        raise VpsError("VM still exists after delete attempt.")


def remove_vps_files(vps):
    """Best-effort removal of a VPS's disk, seed and instance directory."""
    for path in (vps.get("disk_path"), vps.get("seed_path")):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    instance_dir = vps.get("instance_dir")
    if instance_dir and os.path.isdir(instance_dir):
        shutil.rmtree(instance_dir, ignore_errors=True)


async def cleanup_failed(vps):
    """Section 21: never leave broken VMs consuming resources."""
    try:
        await kill_vm(vps["vm_name"])
    except Exception:
        pass
    try:
        delete_vm(vps["vm_name"])
    except Exception:
        pass
    remove_vps_files(vps)
    try:
        db.delete_vps_record(vps["vps_id"])
    except Exception:
        pass
    try:
        db.delete_job(vps["vps_id"])
    except Exception:
        pass
    db.log_audit(vps["discord_user_id"], "deployment_failed_cleanup", vps["vps_id"])


def setup_directories():
    for directory in (
        config.storage_dir,
        config.instances_dir,
        config.keys_dir,
        config.data_dir,
        os.path.dirname(config.database_file),
    ):
        if directory:
            os.makedirs(directory, exist_ok=True)


def ensure_ssh_key():
    """Create a bot SSH keypair once and return the public key."""
    setup_directories()
    if not os.path.exists(config.ssh_priv_key):
        _run(
            [
                "ssh-keygen", "-t", "ed25519", "-N", "",
                "-f", config.ssh_priv_key, "-C", "vpsbot",
            ],
            timeout=30,
        )
    with open(config.ssh_pub_key) as fh:
        return fh.read().strip()
