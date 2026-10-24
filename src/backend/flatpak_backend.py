import subprocess

from .base import PackageBackend


class FlatpakBackend(PackageBackend):
    def install(self, package_names):
        subprocess.run(["flatpak", "install", "-y", "flathub", *package_names], check=True)

    def remove(self, package_names):
        subprocess.run(["flatpak", "uninstall", "-y", *package_names], check=True)

    def is_installed(self, package_name):
        result = subprocess.run(
            ["flatpak", "info", package_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    def add_repo(self, repo_id):
        subprocess.run(
            ["flatpak", "remote-add", "--if-not-exists", repo_id,
             "https://dl.flathub.org/repo/flathub.flatpakrepo"],
            check=True,
        )
