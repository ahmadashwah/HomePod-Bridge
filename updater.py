"""Self-update from GitHub releases.

A release is tagged like "v1.2.0" and carries the built HomePodBridge.exe as an
asset. Updating downloads it next to the running exe, checks it against the
SHA-256 digest GitHub publishes for the asset, swaps the files (Windows allows
renaming a running exe, not overwriting it) and starts the new copy.
"""

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass

ASSET_NAME = "HomePodBridge.exe"
AFTER_UPDATE_FLAG = "--after-update"
_TIMEOUT = 15


@dataclass
class Release:
    version: str
    url: str
    size: int
    sha256: str | None
    notes: str


def parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    while len(parts) < 3:  # "1.0" == "1.0.0"
        parts.append(0)
    return tuple(parts)


def latest_release(repo: str) -> Release | None:
    """The newest published release that has the exe attached, or None."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "HomePodBridge-updater"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            data = json.load(response)
    except urllib.error.HTTPError as ex:
        if ex.code == 404:  # no releases yet
            return None
        raise
    for asset in data.get("assets", []):
        if asset.get("name") == ASSET_NAME:
            digest = asset.get("digest") or ""
            return Release(
                version=data.get("tag_name", "").lstrip("vV"),
                url=asset["browser_download_url"],
                size=int(asset.get("size", 0)),
                sha256=digest.split(":", 1)[1] if digest.startswith("sha256:") else None,
                notes=(data.get("body") or "").strip(),
            )
    return None


def is_newer(release: Release, current: str) -> bool:
    return parse_version(release.version) > parse_version(current)


def can_self_update() -> bool:
    """Only the built exe can replace itself (not a `python homepod_bridge.py` run)."""
    return getattr(sys, "frozen", False)


def install(release: Release) -> None:
    """Download, verify, swap in and start the new exe. The caller then quits."""
    exe = sys.executable
    folder = os.path.dirname(exe)
    new_path = os.path.join(folder, "HomePodBridge.new.exe")
    old_path = os.path.join(folder, "HomePodBridge.old.exe")

    digest = hashlib.sha256()
    request = urllib.request.Request(release.url,
                                     headers={"User-Agent": "HomePodBridge-updater"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response, \
            open(new_path, "wb") as out:
        while chunk := response.read(1 << 16):
            digest.update(chunk)
            out.write(chunk)

    try:
        if release.size and os.path.getsize(new_path) != release.size:
            raise ValueError("the download is incomplete")
        if release.sha256 and digest.hexdigest() != release.sha256.lower():
            raise ValueError("the download doesn't match GitHub's checksum")
    except Exception:
        os.remove(new_path)
        raise

    if os.path.exists(old_path):
        os.remove(old_path)
    os.replace(exe, old_path)  # a running exe can be renamed...
    try:
        os.replace(new_path, exe)  # ...which frees its name for the new one
    except Exception:
        os.replace(old_path, exe)  # put things back
        raise
    # Without this the new copy inherits our PyInstaller settings and tries to
    # run from our unpack folder, which is deleted as we exit ("No module
    # named '_overlapped'" in 1.0.2).
    env = os.environ.copy()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen([exe, AFTER_UPDATE_FLAG], env=env, close_fds=True)


def clean_up_after_update() -> None:
    """Remove the previous exe left behind by an update (called at startup)."""
    if can_self_update():
        old_path = os.path.join(os.path.dirname(sys.executable), "HomePodBridge.old.exe")
        try:
            os.remove(old_path)
        except OSError:
            pass  # not there, or the old process hasn't exited yet
