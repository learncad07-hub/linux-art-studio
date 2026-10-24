"""Unprivileged read-only lookups for DaVinci Resolve and its environment.

No pkexec needed here, same reasoning as unreal_state.py: the helper chowns
anything it creates to the real user, and status reads (SELinux mode, GPU
info) only need file/command access any user already has.
"""
import os
import shutil
import subprocess

INSTALL_DIR = "/opt/resolve"
LAUNCHER_RELATIVE_PATH = "bin/resolve"
UNINSTALLER_RELATIVE_PATH = "installer"


def is_installed():
    return os.path.isdir(INSTALL_DIR)


def launcher_path():
    return os.path.join(INSTALL_DIR, LAUNCHER_RELATIVE_PATH)


def uninstaller_path():
    return os.path.join(INSTALL_DIR, UNINSTALLER_RELATIVE_PATH)


def selinux_mode():
    """Returns 'enforcing', 'permissive', 'disabled', or None if the
    getenforce command isn't available (SELinux not present at all)."""
    getenforce = shutil.which("getenforce")
    if not getenforce:
        return None
    try:
        result = subprocess.run([getenforce], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip().lower() or None


def gpu_status():
    """Best-effort, read-only GPU driver summary. Returns a plain
    description string, never raises: this is informational only, nothing
    here is a precondition for installing or launching Resolve."""
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            result = subprocess.run(
                [nvidia_smi, "--query-gpu=driver_version", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                version = result.stdout.strip().splitlines()[0]
                return f"NVIDIA driver {version} detected"
        except (OSError, subprocess.TimeoutExpired):
            pass

    if shutil.which("vulkaninfo"):
        try:
            result = subprocess.run(
                ["vulkaninfo", "--summary"], capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return "Vulkan driver detected (no NVIDIA driver found)"
        except (OSError, subprocess.TimeoutExpired):
            pass

    return "No GPU driver detected"
