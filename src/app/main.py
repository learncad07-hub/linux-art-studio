import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio

from app.window import MainWindow


class CreativeInstallerApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="local.creativeinstaller.app", flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.connect("activate", self._on_activate)

    def _on_activate(self, app):
        window = self.props.active_window
        if not window:
            window = MainWindow(app)
        window.present()


def main():
    app = CreativeInstallerApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
