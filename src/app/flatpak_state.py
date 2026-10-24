"""Unprivileged read-only lookups for installed Flatpak apps.

No pkexec needed here: `flatpak info` only reads, no root required.
"""
import shutil
import subprocess


def is_installed(app_id):
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return False
    result = subprocess.run(
        [flatpak, "info", app_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0
