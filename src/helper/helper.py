#!/usr/bin/env python3
"""Privileged helper. Invoked as: pkexec python3 helper.py <action> [args...]

Must run unbuffered (python3 -u, or PYTHONUNBUFFERED=1) so the GUI's live
log panel receives output as it happens, not only at exit.
"""
import glob
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.flatpak_backend import FlatpakBackend

UNREAL_INSTALL_PREFIX = "/opt/unreal-engine-"
EDITOR_RELATIVE_PATH = "Engine/Binaries/Linux/UnrealEditor"
UNREAL_ICON_SOURCE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "assets", "icons", "unrealengine-icon.svg",
)

BLENDER_INSTALL_PREFIX = "/opt/blender-"
BLENDER_BINARY_RELATIVE_PATH = "blender"

RESOLVE_INSTALL_DIR = "/opt/resolve"
RESOLVE_DESKTOP_FILE = "/usr/share/applications/com.blackmagicdesign.resolve.desktop"
RESOLVE_ICON_DEST = "/usr/share/icons/hicolor/256x256/apps/com.blackmagicdesign.resolve.png"

# Fedora translation of AlmaLinux Creative Installer's verified EL dependency
# list for DaVinci Resolve (its helper's prepare_resolve_deps). Fedora and EL
# share most of these package names as-is; this is a starting point to be
# confirmed by an actual run, not assumed correct, same lesson as the
# davinci-helper version-string bugs documented in
# skills-and-rules/02-davinci-helper-lessons.md.
RESOLVE_CORE_PACKAGES = [
    "libX11", "libXcursor", "libXext", "libXfixes", "libXi", "libXinerama",
    "libXrandr", "libXrender", "libXt", "libXtst",
    "libxcb", "xcb-util", "xcb-util-image", "xcb-util-keysyms",
    "xcb-util-renderutil", "xcb-util-wm",
    "libxkbcommon", "libxkbcommon-x11",
    "mesa-libGLU", "fontconfig", "freetype",
    "apr", "apr-util", "libxcrypt-compat", "ocl-icd", "libcurl",
]
# Best-effort extras: a missing one here shouldn't abort the install, since
# package names for these have shifted across Fedora releases (fuse/fuse3,
# zlib/zlib-ng-compat).
RESOLVE_OPTIONAL_PACKAGES = ["fuse", "fuse-libs", "zlib"]

RESOLVE_GLIB_LIBS = [
    "libglib-2.0.so", "libglib-2.0.so.0",
    "libgobject-2.0.so", "libgobject-2.0.so.0",
    "libgio-2.0.so", "libgio-2.0.so.0",
    "libgmodule-2.0.so", "libgmodule-2.0.so.0",
    "libgthread-2.0.so", "libgthread-2.0.so.0",
]


def action_ping(args):
    print("pong")
    print(f"helper is alive, running as: uid={os.geteuid()}")
    return 0


def invoking_user():
    """Who ran pkexec, taken from polkit, not from anything the GUI claims."""
    uid = os.environ.get("PKEXEC_UID")
    if uid is None:
        raise RuntimeError("PKEXEC_UID not set. This helper must be run via pkexec.")
    return pwd.getpwuid(int(uid))


def detect_unreal_version(src_dir):
    version_file = os.path.join(src_dir, "Engine", "Build", "Build.version")
    if os.path.isfile(version_file):
        try:
            with open(version_file) as f:
                data = json.load(f)
            major = data["MajorVersion"]
            minor = data["MinorVersion"]
            patch = data.get("PatchVersion", 0)
            return f"{major}.{minor}.{patch}"
        except (OSError, ValueError, KeyError):
            pass

    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", os.path.basename(src_dir))
    if match:
        return match.group(1)

    return None


def extract_archive(zip_path, dest_dir):
    print("Extracting Unreal Engine archive (large, this can take several minutes)...")
    with zipfile.ZipFile(zip_path) as z:
        members = z.infolist()
        total = len(members)
        # Time-based throttling (not percent-change-based): a 1%-wide archive
        # with 300k files only updates once every ~3000 files, which reads
        # as big laggy jumps instead of a live stream. Updating on a fixed
        # wall-clock interval instead keeps it visually continuous no matter
        # how large or small the archive is.
        last_update = 0.0
        for index, member in enumerate(members, start=1):
            z.extract(member, dest_dir)
            now = time.monotonic()
            is_last = index == total
            if now - last_update >= 0.1 or is_last:
                percent = (index * 100) / total if total else 100
                print(f"PROGRESS {percent:.1f}")
                print(f"Extracted {index}/{total} files ({percent:.1f}%)")
                last_update = now


def fix_chrome_sandbox_permissions(install_dir):
    """CEF's embedded Chromium (used by the editor's web-based UI panels)
    needs its chrome-sandbox helper owned by root with the setuid bit set
    to launch its GPU/zygote process. The blanket chown to the real user
    just before this strips that, which doesn't crash immediately but
    makes the GPU process fail to launch every time the editor opens
    (Chromium error_code=1002), taking the whole editor down with it.
    Fix it back up after the ownership pass, not before, since the chown
    loop above would just strip it again if this ran first."""
    pattern = os.path.join(install_dir, "**", "chrome-sandbox")
    for sandbox_path in glob.glob(pattern, recursive=True):
        os.chown(sandbox_path, 0, 0)
        os.chmod(sandbox_path, 0o4755)
        print(f"Fixed CEF sandbox permissions: {sandbox_path}")


def find_engine_src_dir(extracted_dir):
    if os.path.isdir(os.path.join(extracted_dir, "Engine")):
        return extracted_dir

    for entry in sorted(os.listdir(extracted_dir)):
        candidate = os.path.join(extracted_dir, entry)
        if os.path.isdir(candidate) and os.path.isdir(os.path.join(candidate, "Engine")):
            return candidate

    return None


def install_icon(user):
    """Copy the bundled Unreal Engine icon into the user's icon theme so
    desktop launchers can find it by name instead of showing a generic
    fallback icon."""
    if not os.path.isfile(UNREAL_ICON_SOURCE):
        return None

    icon_dir = os.path.join(user.pw_dir, ".local", "share", "icons", "hicolor", "scalable", "apps")
    os.makedirs(icon_dir, exist_ok=True)
    for path in (
        os.path.join(user.pw_dir, ".local"),
        os.path.join(user.pw_dir, ".local", "share"),
        os.path.join(user.pw_dir, ".local", "share", "icons"),
        os.path.join(user.pw_dir, ".local", "share", "icons", "hicolor"),
        os.path.join(user.pw_dir, ".local", "share", "icons", "hicolor", "scalable"),
        icon_dir,
    ):
        os.chown(path, user.pw_uid, user.pw_gid)

    icon_name = "unreal-engine"
    icon_dest = os.path.join(icon_dir, f"{icon_name}.svg")
    shutil.copyfile(UNREAL_ICON_SOURCE, icon_dest)
    os.chown(icon_dest, user.pw_uid, user.pw_gid)
    return icon_name


def write_desktop_file(user, desktop_dir, version, install_dir):
    os.makedirs(desktop_dir, exist_ok=True)
    os.chown(desktop_dir, user.pw_uid, user.pw_gid)

    icon_name = install_icon(user) or "application-x-executable"

    desktop_path = os.path.join(desktop_dir, f"UnrealEngine-{version}.desktop")
    editor_path = os.path.join(install_dir, EDITOR_RELATIVE_PATH)
    content = (
        "[Desktop Entry]\n"
        f"Name=Unreal Engine {version}\n"
        "Comment=Unreal Editor\n"
        f"Exec={editor_path}\n"
        f"Icon={icon_name}\n"
        "Type=Application\n"
        "Terminal=false\n"
        "Categories=Development;Graphics;\n"
        "StartupWMClass=UnrealEditor\n"
    )
    with open(desktop_path, "w") as f:
        f.write(content)
    os.chown(desktop_path, user.pw_uid, user.pw_gid)
    return desktop_path


def action_install_unreal(args):
    if len(args) != 1:
        print("usage: install_unreal <path-to-zip>", file=sys.stderr)
        return 2
    zip_path = args[0]

    if not os.path.isfile(zip_path):
        print(f"ERROR: zip not found: {zip_path}", file=sys.stderr)
        return 1
    if not zip_path.lower().endswith(".zip"):
        print(f"ERROR: expected a .zip archive, got: {zip_path}", file=sys.stderr)
        return 1

    user = invoking_user()

    tmp_dir = tempfile.mkdtemp(prefix=".unreal-tmp-", dir="/opt")
    try:
        extract_archive(zip_path, tmp_dir)

        src_dir = find_engine_src_dir(tmp_dir)
        if src_dir is None:
            print("ERROR: no Engine/ directory found in the archive.", file=sys.stderr)
            return 1

        editor_path = os.path.join(src_dir, EDITOR_RELATIVE_PATH)
        if not os.path.isfile(editor_path):
            print(f"ERROR: {EDITOR_RELATIVE_PATH} not found in the archive.", file=sys.stderr)
            return 1

        version = detect_unreal_version(src_dir)
        if version is None:
            print("ERROR: could not determine the Unreal Engine version from this archive.",
                  file=sys.stderr)
            return 1

        install_dir = f"{UNREAL_INSTALL_PREFIX}{version}"
        if os.path.exists(install_dir):
            print(f"ERROR: {install_dir} already exists. Uninstall that version first "
                  f"if you want to reinstall it.", file=sys.stderr)
            return 1

        print(f"Installing Unreal Engine {version} to {install_dir}...")
        shutil.move(src_dir, install_dir)

        # Python's zipfile extraction does not reliably preserve the
        # executable bit from the archive. Only fixing the main editor
        # binary here isn't enough: the editor shells out to dozens of its
        # own helper binaries (EpicWebHelper for the embedded browser,
        # ShaderCompileWorker, CrashReportClient, UnrealPak, and more), and
        # any of them missing +x fails silently as EACCES deep inside the
        # editor rather than an obvious install error. Fix every ELF file
        # in the tree, not just the one entry point.
        print("Fixing executable permissions (checking the whole tree)...")
        fixed_count = 0
        for root, dirs, files in os.walk(install_dir):
            for name in files:
                path = os.path.join(root, name)
                if os.access(path, os.X_OK):
                    continue
                try:
                    with open(path, "rb") as f:
                        if f.read(4) == b"\x7fELF":
                            os.chmod(path, 0o755)
                            fixed_count += 1
                except OSError:
                    pass
        print(f"Fixed executable permissions on {fixed_count} files.")

        print("Setting ownership (large tree, this can take a moment)...")
        for root, dirs, files in os.walk(install_dir):
            for name in dirs + files:
                os.chown(os.path.join(root, name), user.pw_uid, user.pw_gid)
        os.chown(install_dir, user.pw_uid, user.pw_gid)

        fix_chrome_sandbox_permissions(install_dir)

        desktop_dir = os.path.join(user.pw_dir, ".local", "share", "applications")
        write_desktop_file(user, desktop_dir, version, install_dir)

        print()
        print(f"Done. Unreal Engine {version} installed to {install_dir}.")
        print(f"Launch it from your app menu, or directly: {install_dir}/{EDITOR_RELATIVE_PATH}")
        print("First launch compiles shaders and can take a long time.")
        print(f"INSTALLED_VERSION={version}")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def action_uninstall_unreal(args):
    if len(args) != 1:
        print("usage: uninstall_unreal <version>", file=sys.stderr)
        return 2
    version = args[0]

    user = invoking_user()
    install_dir = f"{UNREAL_INSTALL_PREFIX}{version}"
    desktop_path = os.path.join(user.pw_dir, ".local", "share", "applications",
                                 f"UnrealEngine-{version}.desktop")

    if not os.path.isdir(install_dir):
        print(f"ERROR: {install_dir} does not exist. Nothing to uninstall.", file=sys.stderr)
        return 1

    print(f"Removing {install_dir}...")
    shutil.rmtree(install_dir)

    if os.path.isfile(desktop_path):
        print(f"Removing {desktop_path}...")
        os.remove(desktop_path)

    print(f"Done. Unreal Engine {version} uninstalled.")
    return 0


def extract_blender_tarball(tar_path, dest_dir):
    print("Extracting Blender archive (large, this can take a minute)...")
    # Mode "r" auto-detects the compression (.tar.xz, .tar.bz2, .tar.gz
    # all show up across Blender's real download history, not just the
    # current .tar.xz convention, confirmed by actually checking several
    # real archives from different eras).
    with tarfile.open(tar_path, mode="r") as t:
        members = t.getmembers()
        total = len(members)
        last_update = 0.0
        for index, member in enumerate(members, start=1):
            t.extract(member, dest_dir)
            now = time.monotonic()
            is_last = index == total
            if now - last_update >= 0.1 or is_last:
                percent = (index * 100) / total if total else 100
                print(f"PROGRESS {percent:.1f}")
                print(f"Extracted {index}/{total} files ({percent:.1f}%)")
                last_update = now


def find_blender_src_dir(extracted_dir):
    """Every real Blender Linux tarball (confirmed by actually extracting
    one) has a single top-level folder containing the binary directly,
    named like blender-4.4.3-stable+v44.802179c51ccc-linux.x86_64-release,
    the version always as the first dotted-triple after "blender-"."""
    for entry in sorted(os.listdir(extracted_dir)):
        candidate = os.path.join(extracted_dir, entry)
        if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "blender")):
            return candidate
    return None


def detect_blender_version(src_dir):
    # Modern archives always have a 3-part version (4.4.3); older ones
    # (confirmed by actually checking real downloads) are often only
    # 2-part with an optional lettered point release (2.49b, 2.37a), so
    # the patch number and the letter both have to be optional here.
    match = re.search(r"blender-?(\d+\.\d+(?:\.\d+)?[a-zA-Z]?)", os.path.basename(src_dir))
    return match.group(1) if match else None


def install_blender_icon(user, src_dir):
    """Blender's own tarball already ships a proper icon (blender.svg at
    its top level), unlike Unreal Engine's archive, so there's no need to
    bundle a separate copy of it in this project, just install the one
    that came with the actual version being installed."""
    icon_src = os.path.join(src_dir, "blender.svg")
    if not os.path.isfile(icon_src):
        return None

    icon_dir = os.path.join(user.pw_dir, ".local", "share", "icons", "hicolor", "scalable", "apps")
    os.makedirs(icon_dir, exist_ok=True)
    for path in (
        os.path.join(user.pw_dir, ".local"),
        os.path.join(user.pw_dir, ".local", "share"),
        os.path.join(user.pw_dir, ".local", "share", "icons"),
        os.path.join(user.pw_dir, ".local", "share", "icons", "hicolor"),
        os.path.join(user.pw_dir, ".local", "share", "icons", "hicolor", "scalable"),
        icon_dir,
    ):
        os.chown(path, user.pw_uid, user.pw_gid)

    icon_name = "blender"
    icon_dest = os.path.join(icon_dir, f"{icon_name}.svg")
    shutil.copyfile(icon_src, icon_dest)
    os.chown(icon_dest, user.pw_uid, user.pw_gid)
    return icon_name


def write_blender_desktop_file(user, desktop_dir, version, install_dir, icon_name):
    os.makedirs(desktop_dir, exist_ok=True)
    os.chown(desktop_dir, user.pw_uid, user.pw_gid)

    desktop_path = os.path.join(desktop_dir, f"Blender-{version}.desktop")
    binary_path = os.path.join(install_dir, BLENDER_BINARY_RELATIVE_PATH)
    content = (
        "[Desktop Entry]\n"
        f"Name=Blender {version}\n"
        "Comment=3D modeling, animation, rendering and post-production\n"
        f"Exec={binary_path} %f\n"
        f"Icon={icon_name or 'application-x-executable'}\n"
        "Type=Application\n"
        "Terminal=false\n"
        "Categories=Graphics;3DGraphics;\n"
        "MimeType=application/x-blender;\n"
        "StartupWMClass=Blender\n"
    )
    with open(desktop_path, "w") as f:
        f.write(content)
    os.chown(desktop_path, user.pw_uid, user.pw_gid)
    return desktop_path


BLENDER_ARCHIVE_EXTENSIONS = (".tar.xz", ".tar.bz2", ".tar.gz")


def action_install_blender(args):
    if len(args) != 1:
        print("usage: install_blender <path-to-archive>", file=sys.stderr)
        return 2
    tar_path = args[0]

    if not os.path.isfile(tar_path):
        print(f"ERROR: archive not found: {tar_path}", file=sys.stderr)
        return 1
    if not tar_path.lower().endswith(BLENDER_ARCHIVE_EXTENSIONS):
        print(f"ERROR: expected a .tar.xz/.tar.bz2/.tar.gz archive, got: {tar_path}", file=sys.stderr)
        return 1

    user = invoking_user()

    tmp_dir = tempfile.mkdtemp(prefix=".blender-tmp-", dir="/opt")
    try:
        extract_blender_tarball(tar_path, tmp_dir)

        src_dir = find_blender_src_dir(tmp_dir)
        if src_dir is None:
            print("ERROR: no blender binary found in the archive.", file=sys.stderr)
            return 1

        version = detect_blender_version(src_dir)
        if version is None:
            print("ERROR: could not determine the Blender version from this archive.",
                  file=sys.stderr)
            return 1

        install_dir = f"{BLENDER_INSTALL_PREFIX}{version}"
        if os.path.exists(install_dir):
            print(f"ERROR: {install_dir} already exists. Uninstall that version first "
                  f"if you want to reinstall it.", file=sys.stderr)
            return 1

        print(f"Installing Blender {version} to {install_dir}...")
        shutil.move(src_dir, install_dir)

        # Same lesson as Unreal Engine: tar extraction can also lose the
        # executable bit depending on how the archive was built, and
        # Blender shells out to its own helper binaries too (blender-
        # launcher, blender-thumbnailer), so fix every ELF in the tree,
        # not just the main binary.
        print("Fixing executable permissions (checking the whole tree)...")
        fixed_count = 0
        for root, dirs, files in os.walk(install_dir):
            for name in files:
                path = os.path.join(root, name)
                if os.access(path, os.X_OK):
                    continue
                try:
                    with open(path, "rb") as f:
                        if f.read(4) == b"\x7fELF":
                            os.chmod(path, 0o755)
                            fixed_count += 1
                except OSError:
                    pass
        print(f"Fixed executable permissions on {fixed_count} files.")

        print("Setting ownership (large tree, this can take a moment)...")
        for root, dirs, files in os.walk(install_dir):
            for name in dirs + files:
                os.chown(os.path.join(root, name), user.pw_uid, user.pw_gid)
        os.chown(install_dir, user.pw_uid, user.pw_gid)

        icon_name = install_blender_icon(user, install_dir)
        desktop_dir = os.path.join(user.pw_dir, ".local", "share", "applications")
        write_blender_desktop_file(user, desktop_dir, version, install_dir, icon_name)

        print()
        print(f"Done. Blender {version} installed to {install_dir}.")
        print(f"Launch it from your app menu, or directly: "
              f"{install_dir}/{BLENDER_BINARY_RELATIVE_PATH}")
        print(f"INSTALLED_VERSION={version}")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def action_uninstall_blender(args):
    if len(args) != 1:
        print("usage: uninstall_blender <version>", file=sys.stderr)
        return 2
    version = args[0]

    user = invoking_user()
    install_dir = f"{BLENDER_INSTALL_PREFIX}{version}"
    desktop_path = os.path.join(user.pw_dir, ".local", "share", "applications",
                                 f"Blender-{version}.desktop")

    if not os.path.isdir(install_dir):
        print(f"ERROR: {install_dir} does not exist. Nothing to uninstall.", file=sys.stderr)
        return 1

    print(f"Removing {install_dir}...")
    shutil.rmtree(install_dir)

    if os.path.isfile(desktop_path):
        print(f"Removing {desktop_path}...")
        os.remove(desktop_path)

    print(f"Done. Blender {version} uninstalled.")
    return 0


def action_install_flatpak_app(args):
    if len(args) != 1:
        print("usage: install_flatpak_app <app-id>", file=sys.stderr)
        return 2
    app_id = args[0]

    print(f"Installing {app_id} from Flathub...")
    backend = FlatpakBackend()
    try:
        backend.install([app_id])
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: flatpak install failed: {exc}", file=sys.stderr)
        return 1

    print()
    print(f"Done. {app_id} installed.")
    return 0


def action_uninstall_flatpak_app(args):
    if len(args) != 1:
        print("usage: uninstall_flatpak_app <app-id>", file=sys.stderr)
        return 2
    app_id = args[0]

    print(f"Removing {app_id}...")
    backend = FlatpakBackend()
    try:
        backend.remove([app_id])
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: flatpak uninstall failed: {exc}", file=sys.stderr)
        return 1

    print(f"Done. {app_id} uninstalled.")
    return 0


def action_cancel_install(args):
    if len(args) != 1:
        print("usage: cancel_install <pid>", file=sys.stderr)
        return 2

    try:
        pid = int(args[0])
    except ValueError:
        print(f"ERROR: not a valid pid: {args[0]}", file=sys.stderr)
        return 1

    print(f"Stopping install process {pid}...")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        print(f"Process {pid} was already gone.")
    except PermissionError as exc:
        print(f"ERROR: could not stop process {pid}: {exc}", file=sys.stderr)
        return 1

    removed_any = False
    for pattern in ("/opt/.unreal-tmp-*", "/opt/.blender-tmp-*"):
        for tmp_dir in glob.glob(pattern):
            print(f"Removing partial extraction {tmp_dir}...")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            removed_any = True
    if not removed_any:
        print("No partial extraction directories found.")

    print("Done. Install cancelled.")
    return 0


def set_selinux_enforce(enabled):
    """Temporary only (setenforce), never touches /etc/selinux/config. The
    caller is responsible for restoring the original mode afterward."""
    subprocess.run(["setenforce", "1" if enabled else "0"], check=True)


def resolve_post_install_fixes():
    libs_dir = os.path.join(RESOLVE_INSTALL_DIR, "libs")
    if os.path.isdir(libs_dir):
        for lib in RESOLVE_GLIB_LIBS:
            lib_path = os.path.join(libs_dir, lib)
            backup_path = lib_path + ".bak"
            if os.path.isfile(lib_path) and not os.path.isfile(backup_path):
                os.rename(lib_path, backup_path)
                print(f"Backed up bundled library: {lib} -> {lib}.bak")
    else:
        print(f"Resolve libs dir not found ({libs_dir}); skipping GLib compatibility step.")

    icon_src = None
    for candidate in ("DV_Resolve.png", "resolve.png"):
        candidate_path = os.path.join(RESOLVE_INSTALL_DIR, "graphics", candidate)
        if os.path.isfile(candidate_path):
            icon_src = candidate_path
            break
    if icon_src:
        os.makedirs(os.path.dirname(RESOLVE_ICON_DEST), exist_ok=True)
        shutil.copyfile(icon_src, RESOLVE_ICON_DEST)
        os.chmod(RESOLVE_ICON_DEST, 0o644)
        print("Installed Resolve icon to hicolor theme.")
    else:
        print("Resolve icon source not found under /opt/resolve/graphics; skipping icon copy.")

    if os.path.isfile(RESOLVE_DESKTOP_FILE):
        patch_resolve_desktop_file(RESOLVE_DESKTOP_FILE)
        print("Desktop entry icon and StartupWMClass normalized.")

    print("Post-install fixes completed.")


def patch_resolve_desktop_file(path):
    with open(path) as f:
        lines = f.readlines()

    has_wm_class = False
    new_lines = []
    for line in lines:
        if line.startswith("Icon="):
            new_lines.append("Icon=com.blackmagicdesign.resolve\n")
        elif line.startswith("StartupWMClass="):
            new_lines.append("StartupWMClass=resolve\n")
            has_wm_class = True
        else:
            new_lines.append(line)
    if not has_wm_class:
        new_lines.append("StartupWMClass=resolve\n")

    with open(path, "w") as f:
        f.writelines(new_lines)


def action_install_resolve_deps(args):
    print("Installing dependencies (fuse, apr, libxcrypt-compat, libX11, mesa-libGLU, ...)")
    for package in RESOLVE_OPTIONAL_PACKAGES:
        subprocess.run(["dnf", "-y", "install", package])
    deps_result = subprocess.run(
        ["dnf", "-y", "install", *RESOLVE_CORE_PACKAGES, "--allowerasing"]
    )
    if deps_result.returncode != 0:
        print("ERROR: failed to install required dependencies.", file=sys.stderr)
        return 1
    print("Dependencies installed.")
    return 0


def action_set_resolve_selinux(args):
    # Only the actual setenforce call needs root; reading the current mode
    # doesn't (resolve_state.selinux_mode() on the GUI side handles that).
    # Kept as its own small, fast action, bracketing the installer launch
    # from the GUI side, rather than folded into a single big install
    # action: the installer itself must NOT run through this privileged
    # helper at all. Running it via pkexec+runuser puts it in the root
    # session pkexec creates, not the user's real graphical session, so
    # its own internal privilege-escalation step can never find the
    # already-running graphical polkit agent (confirmed by actually
    # running it and comparing /proc/<pid>/cgroup and loginctl
    # session-status between the two: different logind sessions entirely)
    # and falls back to an unusable text prompt no matter what environment
    # variables are passed through. See skills-and-rules/02-davinci-helper-lessons.md.
    if len(args) != 1 or args[0] not in ("permissive", "enforcing"):
        print("usage: set_resolve_selinux <permissive|enforcing>", file=sys.stderr)
        return 2
    try:
        set_selinux_enforce(args[0] == "enforcing")
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: could not set SELinux to {args[0]}: {exc}", file=sys.stderr)
        return 1
    print(f"SELinux set to {args[0]}.")
    return 0


def action_resolve_post_install_fixes(args):
    if not os.path.isdir(RESOLVE_INSTALL_DIR):
        print(f"ERROR: {RESOLVE_INSTALL_DIR} not found, nothing to fix.", file=sys.stderr)
        return 1
    print("Applying post-install compatibility fixes...")
    resolve_post_install_fixes()
    print("Done.")
    return 0


def action_resolve_cleanup(args):
    """Removes whatever the vendor uninstaller (run unprivileged by the
    GUI beforehand, see resolve_installer.py, same session reasoning as
    the installer) didn't get to, plus the desktop file and icon. Safe to
    call even if the vendor uninstaller already removed everything, or if
    there was no vendor uninstaller to run at all."""
    if os.path.isdir(RESOLVE_INSTALL_DIR):
        print(f"Removing {RESOLVE_INSTALL_DIR}...")
        shutil.rmtree(RESOLVE_INSTALL_DIR, ignore_errors=True)

    if os.path.isfile(RESOLVE_DESKTOP_FILE):
        print(f"Removing {RESOLVE_DESKTOP_FILE}...")
        os.remove(RESOLVE_DESKTOP_FILE)

    if os.path.isfile(RESOLVE_ICON_DEST):
        os.remove(RESOLVE_ICON_DEST)

    print("Done. DaVinci Resolve uninstalled.")
    return 0


def action_install_resolve_codecs(args):
    print("Installing extra codecs (FFmpeg/multimedia group)...")
    # Every non-interactive dnf call here needs its -y flag, this exact
    # omission (a missing -y on the first of two chained commands) is the
    # one real bug recorded against davinci-helper's codec installer, see
    # skills-and-rules/02-davinci-helper-lessons.md.
    upgrade_result = subprocess.run([
        "dnf", "-y", "upgrade", "@multimedia",
        "--setopt=install_weak_deps=False",
        "--exclude=PackageKit-gstreamer-plugin",
    ])
    if upgrade_result.returncode != 0:
        print("ERROR: failed to upgrade the multimedia group.", file=sys.stderr)
        return 1

    group_result = subprocess.run(["dnf", "-y", "group", "install", "sound-and-video"])
    if group_result.returncode != 0:
        print("ERROR: failed to install the sound-and-video group.", file=sys.stderr)
        return 1

    print("Done. Extra codecs installed.")
    return 0


ACTIONS = {
    "ping": action_ping,
    "install_unreal": action_install_unreal,
    "uninstall_unreal": action_uninstall_unreal,
    "install_blender": action_install_blender,
    "uninstall_blender": action_uninstall_blender,
    "install_flatpak_app": action_install_flatpak_app,
    "uninstall_flatpak_app": action_uninstall_flatpak_app,
    "cancel_install": action_cancel_install,
    "install_resolve_deps": action_install_resolve_deps,
    "set_resolve_selinux": action_set_resolve_selinux,
    "resolve_post_install_fixes": action_resolve_post_install_fixes,
    "resolve_cleanup": action_resolve_cleanup,
    "install_resolve_codecs": action_install_resolve_codecs,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ACTIONS:
        print(f"usage: {sys.argv[0]} <action> [args...]", file=sys.stderr)
        print(f"known actions: {', '.join(sorted(ACTIONS))}", file=sys.stderr)
        return 2

    action = sys.argv[1]
    args = sys.argv[2:]
    try:
        return ACTIONS[action](args) or 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
