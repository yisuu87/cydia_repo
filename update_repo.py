#!/usr/bin/env python3
"""
Cydia/Sileo Repo Updater - replaces updaterepo.sh for Windows.

Usage:
    python update_repo.py                  # rebuild index from debians/
    python update_repo.py add <deb> [...]  # import debs then rebuild
"""

import gzip
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
DEBIANS_DIR = REPO_ROOT / "debians"

REPO_ORIGIN = "Yisuu's Repo"
REPO_LABEL = "Yisuu's Repo"
REPO_CODENAME = "ios"
REPO_DESCRIPTION = "A personal Cydia/Sileo repository."


def read_ar_members(filepath):
    with open(filepath, "rb") as f:
        magic = f.read(8)
        if magic != b"!<arch>\n":
            raise ValueError(f"{filepath} is not a valid AR archive")
        while True:
            header = f.read(60)
            if len(header) < 60:
                break
            name = header[0:16].strip().rstrip(b"/")
            size = int(header[48:58].strip())
            data = f.read(size)
            if size % 2 != 0:
                f.read(1)
            yield name.decode("ascii", errors="replace"), size, data


def extract_control_from_deb(deb_path):
    for name, _size, data in read_ar_members(deb_path):
        if name.startswith("control.tar"):
            fileobj = io.BytesIO(data)
            if name.endswith(".gz"):
                mode = "r:gz"
            elif name.endswith(".xz"):
                mode = "r:xz"
            elif name.endswith(".zst"):
                try:
                    import zstandard as zstd
                    fileobj = io.BytesIO(zstd.ZstdDecompressor().decompress(data))
                    mode = "r:"
                except ImportError:
                    raise RuntimeError("pip install zstandard for .zst support")
            elif name.endswith(".bz2"):
                mode = "r:bz2"
            else:
                mode = "r:"
            with tarfile.open(fileobj=fileobj, mode=mode) as tar:
                for member in tar.getmembers():
                    if member.name in ("./control", "control"):
                        f = tar.extractfile(member)
                        if f:
                            return f.read().decode("utf-8", errors="replace")
    raise ValueError(f"No control file found in {deb_path}")


def parse_control(text):
    fields = {}
    key = None
    lines = []
    for line in text.splitlines():
        if line.startswith(" ") or line.startswith("\t"):
            lines.append(line)
        elif ":" in line:
            if key:
                fields[key] = "\n".join(lines)
            k, _, v = line.partition(":")
            key = k.strip()
            lines = [v.strip()]
        else:
            if key:
                lines.append(line)
    if key:
        fields[key] = "\n".join(lines)
    return fields


def compute_hashes(filepath):
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    size = 0
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            md5.update(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
    return {"md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "sha256": sha256.hexdigest(), "size": size}


def generate_packages():
    deb_files = sorted(DEBIANS_DIR.glob("*.deb"))
    if not deb_files:
        print("[!] No .deb files in debians/")
        return

    priority_keys = [
        "Package", "Name", "Version", "Architecture", "Description",
        "Maintainer", "Author", "Section", "Depends", "Conflicts",
        "Replaces", "Provides", "Pre-Depends", "Homepage",
        "Depiction", "SileoDepiction", "Icon",
        "Filename", "Size", "MD5sum", "SHA1", "SHA256", "Installed-Size",
    ]

    entries = []
    for deb in deb_files:
        try:
            ctrl = parse_control(extract_control_from_deb(deb))
            h = compute_hashes(deb)
            ctrl["Filename"] = f"debians/{deb.name}"
            ctrl["Size"] = str(h["size"])
            ctrl["MD5sum"] = h["md5"]
            ctrl["SHA1"] = h["sha1"]
            ctrl["SHA256"] = h["sha256"]

            pkg_id = ctrl.get("Package", "unknown")
            if "SileoDepiction" not in ctrl:
                depiction_dir = REPO_ROOT / "depictions" / "native" / pkg_id
                if depiction_dir.exists():
                    ctrl["SileoDepiction"] = f"depictions/native/{pkg_id}/depiction.json"

            entries.append(ctrl)
            print(f"  [+] {pkg_id} {ctrl.get('Version', '?')}")
        except Exception as e:
            print(f"  [!] {deb.name}: {e}")

    # Build Packages text
    text = ""
    for entry in entries:
        lines = []
        used = set()
        for k in priority_keys:
            if k in entry:
                val = entry[k]
                lines.append(f"{k}: {val}")
                used.add(k)
        for k, v in entry.items():
            if k not in used:
                lines.append(f"{k}: {v}")
        text += "\n".join(lines) + "\n\n"

    # Write with LF line endings
    (REPO_ROOT / "Packages").write_bytes(text.encode("utf-8"))
    (REPO_ROOT / "Packages.gz").write_bytes(gzip.compress(text.encode("utf-8")))
    print(f"\n  [+] Packages ({len(entries)} package(s))")
    print("  [+] Packages.gz")
    return entries


def generate_release():
    from datetime import datetime, timezone
    date_str = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S UTC")

    release = (
        f"Origin: {REPO_ORIGIN}\n"
        f"Label: {REPO_LABEL}\n"
        f"Suite: stable\n"
        f"Version: 1.0\n"
        f"Codename: {REPO_CODENAME}\n"
        f"Architectures: iphoneos-arm iphoneos-arm64\n"
        f"Components: main\n"
        f"Date: {date_str}\n"
        f"Description: {REPO_DESCRIPTION}\n"
    )

    (REPO_ROOT / "Release").write_bytes(release.encode("utf-8"))
    print("  [+] Release")


def sign_release():
    release = REPO_ROOT / "Release"
    gpg_key = REPO_ORIGIN

    for f in ["Release.gpg", "InRelease"]:
        p = REPO_ROOT / f
        if p.exists():
            p.unlink()

    r1 = subprocess.run(
        ["gpg", "--default-key", gpg_key, "-abs", "-o",
         str(REPO_ROOT / "Release.gpg"), str(release)],
        capture_output=True, text=True,
    )
    r2 = subprocess.run(
        ["gpg", "--default-key", gpg_key, "--clearsign", "-o",
         str(REPO_ROOT / "InRelease"), str(release)],
        capture_output=True, text=True,
    )
    if r1.returncode == 0 and r2.returncode == 0:
        print("  [+] GPG signed: Release.gpg + InRelease")
    else:
        print("  [!] GPG signing skipped (no key or gpg not found)")


def main():
    DEBIANS_DIR.mkdir(exist_ok=True)

    # Handle "add" subcommand
    if len(sys.argv) >= 3 and sys.argv[1] == "add":
        for arg in sys.argv[2:]:
            p = Path(arg).resolve()
            if p.is_dir():
                for deb in sorted(p.glob("*.deb")):
                    dest = DEBIANS_DIR / deb.name
                    if dest.resolve() != deb.resolve():
                        shutil.copy2(deb, dest)
                        print(f"  [+] Imported: {deb.name}")
            elif p.is_file() and p.suffix == ".deb":
                dest = DEBIANS_DIR / p.name
                if dest.resolve() != p.resolve():
                    shutil.copy2(p, dest)
                    print(f"  [+] Imported: {p.name}")
            else:
                print(f"  [!] Skipped: {arg}")

    print("\n=== Scanning debians/ ===\n")
    generate_packages()

    print("\n=== Generating Release ===\n")
    generate_release()

    # GPG signing disabled — GitHub Pages corrupts signature files
    # print("\n=== Signing ===\n")
    # sign_release()

    print("\n=== Done! ===")
    print("  git add -A && git commit -m 'Update repo' && git push\n")


if __name__ == "__main__":
    main()
