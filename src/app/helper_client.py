"""Runs the privileged helper via pkexec and streams its output live."""
import os
import subprocess
import sys
import threading
import time

from gi.repository import GLib

HELPER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "helper", "helper.py"
)


def run_helper_action(action, args, on_line, on_done):
    """Start the helper asynchronously.

    on_line(text) and on_done(exit_code) are both called on the GTK main
    thread. Reading and waiting happen on separate background threads: the
    reader fills the log, but completion is decided by waiting on the
    process itself (process.wait(), via the kernel's wait()/waitpid, not
    the pipe). If something else keeps the pipe's write end open after our
    process has actually exited (observed once after a disk-full install
    died mid-extraction), a pipe-EOF-only reader hangs forever even though
    the install is long dead. Waiting on the process directly can't hang
    that way, so on_done always fires once the process truly exits.
    """
    # pkexec wipes almost the entire environment before running the target
    # command (security behavior, not a bug) -- it does not reliably pass
    # PYTHONUNBUFFERED through, so Python's stdout stays block-buffered
    # inside the root process and nothing reaches the GUI until the buffer
    # fills or the process exits. The "-u" flag on the interpreter can't be
    # stripped the same way, so it's used instead of relying on env.
    start_time = time.monotonic()
    print(f"[pkexec] spawning action={action} args={args}", file=sys.stderr, flush=True)

    process = subprocess.Popen(
        ["pkexec", "python3", "-u", HELPER_PATH, action, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    print(f"[pkexec] spawned pid={process.pid}", file=sys.stderr, flush=True)

    def reader():
        try:
            for line in process.stdout:
                GLib.idle_add(on_line, line.rstrip("\n"))
        except (ValueError, OSError):
            pass

    def waiter():
        exit_code = process.wait()
        elapsed = time.monotonic() - start_time
        print(f"[pkexec] pid={process.pid} exited code={exit_code} after {elapsed:.2f}s",
              file=sys.stderr, flush=True)
        GLib.idle_add(on_done, exit_code)

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=waiter, daemon=True).start()
    return process
