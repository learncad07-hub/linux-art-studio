"""Fetches the list of available Blender Linux builds directly from
Blender's own servers, no scraping workarounds needed: both pages below
are plain server-rendered HTML (confirmed with a bare curl, no
JavaScript execution required).

BUILDS_URL (builder.blender.org) lists only the currently active
branches: Stable, Release Candidate, Beta, and Alpha, each with a direct
CDN download link that already encodes version, variant, and platform in
its filename.

RELEASE_INDEX_URL (download.blender.org) is Blender's full release
archive back to version 1.0, one subfolder per version. Its naming is
genuinely inconsistent across eras (confirmed by actually checking
several real folders): modern releases use
blender-X.Y.Z-linux-x64.tar.xz, the 2.7x era used
blender-X.Y-linux-glibcNNN-x86_64.tar.bz2, and pre-2.5 releases often
only shipped 32-bit builds as blenderX.Y-linux-glibcZ.Z.Z-i386.tar.gz,
sometimes with no dash between "blender" and the version at all. Picking
"the" Linux build per folder has to allow for all of that, not just the
current convention.
"""
import concurrent.futures
import re
import urllib.request

BUILDS_URL = "https://builder.blender.org/download/daily/"
RELEASE_INDEX_URL = "https://download.blender.org/release/"

# e.g. blender-4.4.3-stable+v44.802179c51ccc-linux.x86_64-release.tar.xz
# or   blender-5.4.0-alpha+main.b1d561a6c0d6-linux.x86_64-release.tar.xz
LINUX_TARBALL_RE = re.compile(
    r"(?P<url>https?://cdn\.builder\.blender\.org/download/daily/"
    r"blender-(?P<version>\d+\.\d+\.\d+)-(?P<variant>stable|candidate|beta|alpha)"
    r"\+[^\"'<> ]*-linux\.x86_64-release\.tar\.xz)"
)

VARIANT_LABELS = {
    "stable": "Stable",
    "candidate": "Release Candidate",
    "beta": "Beta",
    "alpha": "Alpha",
    "archive": "Archive",
}

# Confirmed against blender.org/download/lts/ directly, not guessed:
# every branch Blender has ever designated Long Term Support, currently
# maintained or not. LTS status applies to the whole X.Y branch (every
# patch release under it), not one exact version, so matching is by
# major.minor.
LTS_BRANCHES = {"2.83", "2.93", "3.3", "3.6", "4.2", "4.5", "5.2"}


def _major_minor(version):
    match = re.match(r"(\d+\.\d+)", version)
    return match.group(1) if match else version


def _is_lts(version):
    return _major_minor(version) in LTS_BRANCHES

# Linux download links inside a release-archive folder, across every
# naming convention actually seen (verified against real folders, not
# guessed): blender-X.Y.Z-linux-x64.tar.xz, blender-X.Y-linux-glibc219-
# x86_64.tar.bz2, blenderX.Ya-linux-glibc2.1.2-i386-static.tar.gz, etc.
ARCHIVE_LINUX_FILE_RE = re.compile(
    r"^blender[-]?\d[\w.]*-linux[-.][\w.-]*\.(?:tar\.xz|tar\.bz2|tar\.gz)$",
    re.IGNORECASE,
)


def _version_sort_key(version):
    """Numeric-first sort key for version strings of varying shapes
    ("1.80", "2.56a", "4.4.3"): trailing letters are stripped, missing
    components padded with 0, so "1.80" sorts below "2.04" and "4.4.3"
    sorts above "4.4" as expected, without crashing on the lettered
    suffixes real old Blender versions actually used."""
    numeric = re.match(r"\d+(?:\.\d+){0,2}", version)
    parts = numeric.group(0).split(".") if numeric else ["0"]
    parts = [int(p) for p in parts] + [0] * (3 - len(parts))
    return tuple(-p for p in parts)


def fetch_linux_builds():
    """Returns a list of dicts, one per currently active Linux x86_64
    build (Stable/Release Candidate/Beta/Alpha only, not the historical
    archive, see fetch_archive_builds for that): {version, variant,
    variant_label, url, filename}, sorted purely by version number
    descending (the newest build first, whatever variant it happens to
    be), not grouped by variant first: with several branches active at
    once (an older LTS, a current stable, an upcoming beta/RC, next
    release's alpha) the highest version number is usually what someone
    means by "the latest". Raises on network/parse failure, the caller
    decides how to surface that."""
    request = urllib.request.Request(BUILDS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        html = response.read().decode("utf-8", errors="replace")

    builds = []
    seen_urls = set()
    for match in LINUX_TARBALL_RE.finditer(html):
        url = match.group("url")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        version = match.group("version")
        variant = match.group("variant")
        builds.append({
            "version": version,
            "variant": variant,
            "variant_label": VARIANT_LABELS[variant],
            "is_lts": _is_lts(version),
            "url": url,
            "filename": url.rsplit("/", 1)[-1],
        })

    builds.sort(key=lambda b: _version_sort_key(b["version"]))
    return builds


def _pick_best_archive_linux_file(filenames):
    """From one release folder's file listing, pick the single best Linux
    download: prefer 64-bit over 32-bit, and .tar.xz over .tar.bz2 over
    .tar.gz, and a non-"static" build over a static one."""
    candidates = [f for f in filenames if ARCHIVE_LINUX_FILE_RE.match(f)]
    if not candidates:
        return None

    def score(name):
        lower = name.lower()
        arch = 2 if ("x86_64" in lower or "x64" in lower or "amd64" in lower) \
            else (1 if ("i386" in lower or "i686" in lower) else 0)
        ext = 3 if lower.endswith(".tar.xz") else (2 if lower.endswith(".tar.bz2") else 1)
        not_static = 0 if "static" in lower else 1
        return (arch, ext, not_static)

    return max(candidates, key=score)


ARCHIVE_FILENAME_VERSION_RE = re.compile(r"^blender[-]?(\d[\w.]*?)-linux", re.IGNORECASE)


def _fetch_archive_folder(folder_name):
    folder_url = f"{RELEASE_INDEX_URL}{folder_name}/"
    request = urllib.request.Request(folder_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            html = response.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    filenames = re.findall(r'href="([^"/?]+)"', html)
    best = _pick_best_archive_linux_file(filenames)
    if not best:
        return None

    # The version in the filename itself is more precise than the folder
    # name: lettered point releases (2.49, 2.49a, 2.49b) all live in the
    # same folder, and at least one folder's actual build is a different
    # version than its own name suggests (Blender2.39/ contains
    # blender-2.40alpha1-...), confirmed by actually checking.
    version_match = ARCHIVE_FILENAME_VERSION_RE.match(best)
    if version_match:
        version = version_match.group(1)
    else:
        version_match = re.match(r"\d+(?:\.\d+){0,2}", folder_name[len("Blender"):])
        version = version_match.group(0) if version_match else folder_name[len("Blender"):]

    return {
        "version": version,
        "variant": "archive",
        "variant_label": "Archive",
        "is_lts": _is_lts(version),
        "url": folder_url + best,
        "filename": best,
    }


def fetch_archive_builds(max_workers=16):
    """Returns the full historical release list back to 1.0 (confirmed:
    89 version folders as of this writing), one Linux build per version,
    picked by _pick_best_archive_linux_file. Fetches every folder's
    listing concurrently, confirmed to take ~3 seconds total for the
    whole history, not one request at a time. Raises on failure to even
    load the top-level index; a single folder failing just drops that one
    version rather than failing the whole fetch."""
    request = urllib.request.Request(RELEASE_INDEX_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        html = response.read().decode("utf-8", errors="replace")

    folder_names = re.findall(r'href="(Blender\d[^"/]*)/"', html)

    builds = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for result in executor.map(_fetch_archive_folder, folder_names):
            if result:
                builds.append(result)

    builds.sort(key=lambda b: _version_sort_key(b["version"]))
    return builds
