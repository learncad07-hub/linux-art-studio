"""Unprivileged read-only lookups for installed Unreal Engine versions.

No pkexec needed here. The helper chowns each install to the real user,
so the GUI can just read /opt directly as itself.
"""
import os

INSTALL_PREFIX = "unreal-engine-"
EDITOR_RELATIVE_PATH = "Engine/Binaries/Linux/UnrealEditor"


def list_installed_versions():
    opt_dir = "/opt"
    if not os.path.isdir(opt_dir):
        return []

    versions = []
    for entry in sorted(os.listdir(opt_dir)):
        if not entry.startswith(INSTALL_PREFIX):
            continue
        install_dir = os.path.join(opt_dir, entry)
        if os.path.isdir(install_dir):
            versions.append((entry[len(INSTALL_PREFIX):], install_dir))
    return versions


def editor_path(install_dir):
    return os.path.join(install_dir, EDITOR_RELATIVE_PATH)
