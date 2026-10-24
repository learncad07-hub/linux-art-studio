import os
import time

LOG_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "creativeinstaller", "logs")


def open_log_file(action_name):
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(LOG_DIR, f"{action_name}-{timestamp}.log")
    return path, open(path, "a")
