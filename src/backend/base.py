from abc import ABC, abstractmethod


class PackageBackend(ABC):
    """Common interface every distro-family backend implements."""

    @abstractmethod
    def install(self, package_names):
        """Install one or more packages. Raises on failure."""
        raise NotImplementedError

    @abstractmethod
    def remove(self, package_names):
        """Remove one or more packages. Raises on failure."""
        raise NotImplementedError

    @abstractmethod
    def is_installed(self, package_name):
        raise NotImplementedError

    @abstractmethod
    def add_repo(self, repo_id):
        raise NotImplementedError


def detect_backend():
    """Pick the right backend for the running system, from /etc/os-release."""
    os_release = {}
    with open("/etc/os-release") as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os_release[key] = value.strip('"')

    ids = {os_release.get("ID", "")} | set(os_release.get("ID_LIKE", "").split())

    if ids & {"fedora", "rhel", "centos"}:
        from .dnf_backend import DnfBackend
        return DnfBackend()

    raise RuntimeError(
        f"No package backend available for this distro (ID={os_release.get('ID')!r}, "
        f"ID_LIKE={os_release.get('ID_LIKE')!r})."
    )
