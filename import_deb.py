#!/usr/bin/env python3
"""
Cydia/Sileo Repo - One-click deb importer.

Usage:
    python3 import_deb.py <deb_file_or_directory>
    python3 import_deb.py package.deb
    python3 import_deb.py /path/to/folder/with/debs

This script:
  1. Copies .deb files into ./debs/
  2. Extracts control info from each .deb (ar + tar, pure Python)
  3. Generates Packages, Packages.bz2, Packages.gz
  4. Updates Release file with checksums
  5. Generates a basic Sileo-native depiction (JSON) per package
"""

import hashlib
import io
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import json
import glob
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
DEBS_DIR = REPO_ROOT / "debs"
DEPICTIONS_DIR = REPO_ROOT / "depictions"


# ---------------------------------------------------------------------------
# AR archive reader (pure Python, no dpkg dependency)
# ---------------------------------------------------------------------------

def read_ar_members(filepath: Path):
    """Yield (name, size, data) for each member in an AR archive."""
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

            # AR entries are 2-byte aligned
            if size % 2 != 0:
                f.read(1)

            yield name.decode("ascii", errors="replace"), size, data


def extract_control_from_deb(deb_path: Path) -> str:
    """Extract the 'control' file content from a .deb package."""
    for name, _size, data in read_ar_members(deb_path):
        if name.startswith("control.tar"):
            fileobj = io.BytesIO(data)

            # Determine compression
            if name.endswith(".gz"):
                mode = "r:gz"
            elif name.endswith(".xz"):
                mode = "r:xz"
            elif name.endswith(".zst"):
                # Python tarfile doesn't support zstd natively
                try:
                    import zstandard as zstd
                    dctx = zstd.ZstdDecompressor()
                    decompressed = dctx.decompress(data)
                    fileobj = io.BytesIO(decompressed)
                    mode = "r:"
                except ImportError:
                    raise RuntimeError(
                        "zstandard module required for .zst debs: pip install zstandard"
                    )
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


def parse_control(control_text: str) -> dict:
    """Parse a Debian control file into a dict."""
    fields = {}
    current_key = None
    current_value_lines = []

    for line in control_text.splitlines():
        if line.startswith(" ") or line.startswith("\t"):
            # Continuation line
            current_value_lines.append(line)
        elif ":" in line:
            # Save previous field
            if current_key:
                fields[current_key] = "\n".join(current_value_lines)
            key, _, value = line.partition(":")
            current_key = key.strip()
            current_value_lines = [value.strip()]
        else:
            if current_key:
                current_value_lines.append(line)

    if current_key:
        fields[current_key] = "\n".join(current_value_lines)

    return fields


def compute_hashes(filepath: Path):
    """Compute MD5, SHA1, SHA256 and file size."""
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

    return {
        "md5": md5.hexdigest(),
        "sha1": sha1.hexdigest(),
        "sha256": sha256.hexdigest(),
        "size": size,
    }


# ---------------------------------------------------------------------------
# Import .deb files
# ---------------------------------------------------------------------------

def import_deb(deb_path: Path) -> dict | None:
    """Copy a .deb into debs/ and return its control fields."""
    dest = DEBS_DIR / deb_path.name
    if dest.resolve() != deb_path.resolve():
        shutil.copy2(deb_path, dest)
        print(f"  [+] Copied: {deb_path.name}")
    else:
        print(f"  [=] Already in debs/: {deb_path.name}")

    try:
        control_text = extract_control_from_deb(dest)
    except Exception as e:
        print(f"  [!] Failed to read {deb_path.name}: {e}")
        return None

    fields = parse_control(control_text)
    hashes = compute_hashes(dest)

    fields["Filename"] = f"debs/{deb_path.name}"
    fields["Size"] = str(hashes["size"])
    fields["MD5sum"] = hashes["md5"]
    fields["SHA1"] = hashes["sha1"]
    fields["SHA256"] = hashes["sha256"]

    return fields


# ---------------------------------------------------------------------------
# Generate Packages file
# ---------------------------------------------------------------------------

def generate_packages():
    """Scan all debs in debs/ and generate Packages index."""
    deb_files = sorted(DEBS_DIR.glob("*.deb"))
    if not deb_files:
        print("[!] No .deb files found in debs/")
        return []

    all_entries = []
    for deb_file in deb_files:
        try:
            control_text = extract_control_from_deb(deb_file)
            fields = parse_control(control_text)
            hashes = compute_hashes(deb_file)

            fields["Filename"] = f"debs/{deb_file.name}"
            fields["Size"] = str(hashes["size"])
            fields["MD5sum"] = hashes["md5"]
            fields["SHA1"] = hashes["sha1"]
            fields["SHA256"] = hashes["sha256"]

            all_entries.append(fields)
        except Exception as e:
            print(f"  [!] Skipping {deb_file.name}: {e}")

    # Write Packages file
    packages_text = ""
    # Field output order (Debian standard)
    priority_keys = [
        "Package", "Name", "Version", "Architecture", "Description",
        "Maintainer", "Author", "Section", "Depends", "Conflicts",
        "Replaces", "Provides", "Homepage", "Depiction", "SileoDepiction",
        "Icon", "Filename", "Size", "MD5sum", "SHA1", "SHA256",
    ]

    for entry in all_entries:
        # Add Sileo depiction URL if not present
        pkg_id = entry.get("Package", "unknown")
        if "SileoDepiction" not in entry:
            entry["SileoDepiction"] = f"depictions/{pkg_id}.json"

        lines = []
        used_keys = set()
        for key in priority_keys:
            if key in entry:
                lines.append(f"{key}: {entry[key]}")
                used_keys.add(key)
        # Append remaining fields
        for key, value in entry.items():
            if key not in used_keys:
                lines.append(f"{key}: {value}")

        packages_text += "\n".join(lines) + "\n\n"

    packages_path = REPO_ROOT / "Packages"
    packages_path.write_bytes(packages_text.encode("utf-8"))
    print(f"  [+] Generated Packages ({len(all_entries)} package(s))")

    # Compress
    try:
        import gzip
        gz_path = REPO_ROOT / "Packages.gz"
        gz_path.write_bytes(gzip.compress(packages_text.encode("utf-8")))
        print("  [+] Generated Packages.gz")
    except Exception as e:
        print(f"  [!] gzip compression failed: {e}")

    return all_entries


# ---------------------------------------------------------------------------
# Generate Sileo depictions
# ---------------------------------------------------------------------------

def generate_depiction(fields: dict):
    """Generate a Sileo-native JSON depiction for a package."""
    pkg_id = fields.get("Package", "unknown")
    name = fields.get("Name", pkg_id)
    version = fields.get("Version", "1.0")
    description = fields.get("Description", "No description available.")
    author = fields.get("Author", fields.get("Maintainer", "Unknown"))
    section = fields.get("Section", "Tweaks")

    depiction = {
        "minVersion": "0.1",
        "headerImage": "",
        "tintColor": "#2CB1BE",
        "tabs": [
            {
                "tabname": "Details",
                "views": [
                    {
                        "class": "DepictionMarkdownView",
                        "markdown": f"# {name}\n\n{description}",
                        "useSpacing": True,
                    },
                    {
                        "class": "DepictionSeparatorView",
                    },
                    {
                        "class": "DepictionTableTextView",
                        "title": "Version",
                        "text": version,
                    },
                    {
                        "class": "DepictionTableTextView",
                        "title": "Author",
                        "text": author,
                    },
                    {
                        "class": "DepictionTableTextView",
                        "title": "Section",
                        "text": section,
                    },
                ],
            },
            {
                "tabname": "Changelog",
                "views": [
                    {
                        "class": "DepictionMarkdownView",
                        "markdown": f"### {version}\n\n- Initial release.",
                        "useSpacing": True,
                    },
                ],
            },
        ],
        "class": "DepictionTabView",
    }

    depiction_path = DEPICTIONS_DIR / f"{pkg_id}.json"
    depiction_path.write_bytes(
        json.dumps(depiction, indent=2, ensure_ascii=False).encode("utf-8")
    )
    print(f"  [+] Depiction: depictions/{pkg_id}.json")


# ---------------------------------------------------------------------------
# Update Release file with checksums
# ---------------------------------------------------------------------------

def update_release():
    """Update Release with checksums and GPG sign."""
    release_path = REPO_ROOT / "Release"
    release_text = release_path.read_bytes().decode("utf-8")

    # Remove old checksum sections
    lines = []
    skip = False
    for line in release_text.splitlines():
        if line.startswith("MD5Sum:") or line.startswith("SHA1:") or line.startswith("SHA256:"):
            skip = True
            continue
        if skip and line.startswith(" "):
            continue
        skip = False
        lines.append(line)

    # Update Date
    from datetime import datetime, timezone
    date_str = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S UTC")
    new_lines = []
    date_found = False
    for line in lines:
        if line.startswith("Date:"):
            new_lines.append(f"Date: {date_str}")
            date_found = True
        else:
            new_lines.append(line)
    if not date_found:
        # Insert before Description
        idx = next((i for i, l in enumerate(new_lines) if l.startswith("Description:")), len(new_lines))
        new_lines.insert(idx, f"Date: {date_str}")
    lines = new_lines

    release_path.write_bytes("\n".join(lines).encode("utf-8"))
    print("  [+] Updated Release (no checksums — GitHub Pages alters files)")

    # GPG sign
    gpg_key = "Yisuu Repo"
    release_gpg = REPO_ROOT / "Release.gpg"
    in_release = REPO_ROOT / "InRelease"
    release_gpg.unlink(missing_ok=True)
    in_release.unlink(missing_ok=True)

    ret1 = subprocess.run(
        ["gpg", "--default-key", gpg_key, "-abs", "-o", str(release_gpg), str(release_path)],
        capture_output=True, text=True,
    )
    ret2 = subprocess.run(
        ["gpg", "--default-key", gpg_key, "--clearsign", "-o", str(in_release), str(release_path)],
        capture_output=True, text=True,
    )
    if ret1.returncode == 0 and ret2.returncode == 0:
        print("  [+] GPG signed: Release.gpg + InRelease")
    else:
        print("  [!] GPG signing failed (install gpg and generate key)")
        if ret1.stderr:
            print(f"      {ret1.stderr.strip()}")
        if ret2.stderr:
            print(f"      {ret2.stderr.strip()}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 import_deb.py <deb_file_or_directory> [...]")
        print()
        print("Examples:")
        print("  python3 import_deb.py mypackage.deb")
        print("  python3 import_deb.py /path/to/debs/")
        print("  python3 import_deb.py pkg1.deb pkg2.deb pkg3.deb")
        sys.exit(1)

    DEBS_DIR.mkdir(exist_ok=True)
    DEPICTIONS_DIR.mkdir(exist_ok=True)

    # Collect all .deb paths
    deb_paths = []
    for arg in sys.argv[1:]:
        p = Path(arg).resolve()
        if p.is_dir():
            deb_paths.extend(sorted(p.glob("*.deb")))
        elif p.is_file() and p.suffix == ".deb":
            deb_paths.append(p)
        else:
            print(f"[!] Skipping: {arg} (not a .deb file or directory)")

    if not deb_paths:
        print("[!] No .deb files found in the given arguments.")
        sys.exit(1)

    print(f"\n=== Importing {len(deb_paths)} deb(s) ===\n")
    for deb_path in deb_paths:
        result = import_deb(deb_path)
        if result:
            print(f"  [i] {result.get('Package', '?')} {result.get('Version', '?')}")

    print("\n=== Rebuilding repository index ===\n")
    all_entries = generate_packages()

    print("\n=== Generating Sileo depictions ===\n")
    for entry in all_entries:
        generate_depiction(entry)

    print("\n=== Updating Release ===\n")
    update_release()

    print("\n=== Done! ===")
    print(f"  Packages in repo: {len(all_entries)}")
    print("  Next steps:")
    print("    1. git add -A && git commit -m 'Update repo'")
    print("    2. git push")
    print("    3. Enable GitHub Pages (Settings > Pages > main branch)")
    print("    4. Add source in Cydia/Sileo: https://<username>.github.io/<repo>/")
    print()


if __name__ == "__main__":
    main()
