"""Runs the DaVinci Resolve vendor .run installer/uninstaller directly,
as this process's own user, in this process's own session.

Must NOT go through pkexec or runuser: the vendor tooling's own internal
privilege-escalation step is itself a pkexec call, and that only finds a
registered graphical polkit agent when it is a descendant of the real
user session the agent is registered in. pkexec creates a brand new root
session for anything it runs, and runuser dropping back to the invoking
user afterward does not reattach the process to the original session
(confirmed by comparing /proc/<pid>/cgroup and loginctl session-status
between a normal GUI process and one spawned this way: different logind
sessions entirely), so launching the vendor tooling from inside the
privileged helper always hits this, no matter what environment variables
are passed through. Only the dependency install, SELinux toggle, and
post-install fixes actually need root; those stay in helper.py.
"""
import errno
import fcntl
import os
import pty
import subprocess
import termios
import time
import zipfile


def extract_zip(zip_path, dest_dir, on_progress):
    with zipfile.ZipFile(zip_path) as z:
        members = z.infolist()
        total = len(members)
        last_update = 0.0
        for index, member in enumerate(members, start=1):
            z.extract(member, dest_dir)
            now = time.monotonic()
            is_last = index == total
            if now - last_update >= 0.1 or is_last:
                percent = (index * 100) / total if total else 100
                on_progress(percent, index, total)
                last_update = now


def find_run_installer(extracted_dir):
    """Blackmagic's zip contains the .run installer directly at its top
    level alongside an install-instructions HTML file, no subfolder
    nesting (confirmed by actually extracting a real download), but check
    one level of subdirectories too in case a future archive nests it."""
    for entry in sorted(os.listdir(extracted_dir)):
        if entry.lower().endswith(".run"):
            candidate = os.path.join(extracted_dir, entry)
            if os.path.isfile(candidate):
                return candidate

    for entry in sorted(os.listdir(extracted_dir)):
        sub_dir = os.path.join(extracted_dir, entry)
        if os.path.isdir(sub_dir):
            for sub_entry in sorted(os.listdir(sub_dir)):
                if sub_entry.lower().endswith(".run"):
                    candidate = os.path.join(sub_dir, sub_entry)
                    if os.path.isfile(candidate):
                        return candidate

    return None


def run_with_pty(path, on_line):
    """Run a vendor executable with a real pseudo-terminal, not just a
    pipe (it prints plain y/n prompts on its own stdin, and may need a
    real controlling tty for other reasons too), auto-answering any
    "(y/n)" prompt since there's no human at the keyboard. Allocates a
    pty and makes the child its own session leader with the slave end as
    its controlling tty (TIOCSCTTY). Returns the child's exit code."""
    os.chmod(path, 0o755)
    master_fd, slave_fd = pty.openpty()

    def make_controlling_tty():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    env = os.environ.copy()
    env["SKIP_PACKAGE_CHECK"] = "1"

    process = subprocess.Popen(
        [path],
        env=env,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=make_controlling_tty,
        close_fds=True,
    )
    os.close(slave_fd)

    buffer = b""
    try:
        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break  # the child closed its end of the pty
                raise
            if not chunk:
                break
            on_line(chunk.decode(errors="replace"))
            buffer += chunk
            if b"(y/n)" in buffer:
                os.write(master_fd, b"y\n")
                buffer = b""
    finally:
        os.close(master_fd)

    return process.wait()
