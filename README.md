# Linux Art Studio

A guided, live-logged installer for creative and development tools that
don't have good native Fedora packages, starting with Unreal Engine and
DaVinci Resolve.

> The name is a placeholder and may change.

## Why

Some of the best creative and game-dev tools on Linux are distributed as
plain archives or vendor installers with no Fedora package at all. Getting
them installed correctly, with working desktop launchers, correct taskbar
icons, and the right post-install fixes, usually means following a wiki
page and running commands by hand. Linux Art Studio wraps that process in
a GTK4/libadwaita app with a live log, so install and uninstall both go
through the GUI.

## Features

- **Unreal Engine**: install and run multiple versions side by side, pick
  the downloaded Epic `.zip`, live install log, clean uninstall per
  version.
- **DaVinci Resolve**: dependency install, SELinux handling during the
  vendor installer, post-install fixes (GPU driver check, desktop icon,
  optional extra codecs), clean uninstall.
- **Blender**: browse and download available versions directly (stable,
  RC, beta, alpha, and the full historical archive back to 1.80) or
  install from a local file, multiple versions side by side.
- **Flatpak catalog**: one-click install/uninstall for a growing list of
  creative and dev tools via Flathub, including Epic Asset Manager, Asset
  Manager Studio, FreeCAD, MeshLab, PrusaSlicer, Kdenlive, Shotcut, OBS
  Studio, Natron, GIMP, Inkscape, Krita, darktable, RawTherapee, Scribus,
  Ardour, and Audacity.

## Architecture

- An unprivileged GTK4/libadwaita frontend (`src/app/`) that never runs
  as root.
- A separate privileged helper script (`src/helper/`), invoked through
  `pkexec` with a dedicated polkit policy (`data/polkit/`), doing all
  actual system changes (package installs, writing to `/opt`, desktop
  entries). Its stdout/stderr stream live into the app's log panel.
  Vendor installers that self-elevate (like DaVinci Resolve's) run
  directly from the GUI process instead, so their own `pkexec` prompt
  reaches the user's real desktop session.
- A small package-manager backend interface (`src/backend/`), with a
  `DnfBackend` and `FlatpakBackend` implementation. Distro family is
  detected from `/etc/os-release`, not string-matched against a specific
  release name, so it isn't tied to one Fedora version.
- Apps are described as declarative data (`src/data/apps.py`): name,
  category, icon, and backend, rather than hardcoded per-app logic
  scattered through the UI.

## Status

Fedora-first. Install/uninstall flows for Unreal Engine, DaVinci Resolve,
Blender, and the Flatpak catalog have been built and tested on Fedora
Linux with KDE Plasma. Other distro families (`AptBackend`/
`PacmanBackend`) are not implemented yet; the backend interface is built
so they can be added without restructuring the rest of the app.

## Requirements

- Fedora Linux (or a close derivative)
- Python 3 with PyGObject
- GTK4 and libadwaita
- `pkexec` / polkit
- `dnf` and `flatpak`

## Running from source

```
python3 -m venv .venv
source .venv/bin/activate
PYTHONPATH=src python3 src/app/main.py
```

The polkit policy in `data/polkit/` needs to be installed to
`/usr/share/polkit-1/actions/` (with its helper path annotation pointing
at wherever `src/helper/helper.py` actually lives) before privileged
actions will work.

## License

GPLv3. See [LICENSE](LICENSE).
