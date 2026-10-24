import subprocess

from .base import PackageBackend


class DnfBackend(PackageBackend):
    def install(self, package_names):
        subprocess.run(["dnf", "-y", "install", *package_names], check=True)

    def remove(self, package_names):
        subprocess.run(["dnf", "-y", "remove", *package_names], check=True)

    def is_installed(self, package_name):
        result = subprocess.run(
            ["rpm", "-q", package_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    def add_repo(self, repo_id):
        subprocess.run(["dnf", "-y", "copr", "enable", repo_id], check=True)
