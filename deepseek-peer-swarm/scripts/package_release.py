"""Build and verify a GitHub source ZIP without including private local state."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tomllib
import zipfile

from check_publication import ROOT, candidates, scan_file


def main() -> int:
    files = [path for path in candidates(ROOT) if path.exists() or path.is_symlink()]
    findings = [finding for path in files for finding in scan_file(ROOT, path)]
    if findings:
        print("Packaging stopped; publication findings (values withheld):")
        print("\n".join(findings))
        return 1
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    name, version = project["name"], project["version"]
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) for value in (name, version)):
        raise ValueError("Project name and version must be safe archive names")
    relative_names = {path.relative_to(ROOT).as_posix() for path in files}
    required = {"LICENSE", "README.md", "pyproject.toml", "requirements.lock", ".gitignore",
                ".gitattributes", ".github/workflows/checks.yml", "swarm/__main__.py",
                "swarm/static/index.html", "swarm/prompts/peer.md", "docs/GITHUB_UPLOAD.md"}
    if missing := required - relative_names:
        raise ValueError("Required release files missing: " + ", ".join(sorted(missing)))

    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    archive = output / f"{name}-{version}-github.zip"
    temporary = archive.with_suffix(".zip.tmp")
    expected = {}
    # Fixed timestamps and modes avoid publishing local filesystem metadata and
    # make identical source trees produce identical archives.
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in files:
            member = f"{name}/{path.relative_to(ROOT).as_posix()}"
            data = path.read_bytes()
            expected[member] = hashlib.sha256(data).hexdigest()
            info = zipfile.ZipInfo(member, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data, compresslevel=9)
    with zipfile.ZipFile(temporary) as bundle:
        if bundle.testzip() is not None or set(bundle.namelist()) != set(expected):
            raise ValueError("Archive contents failed verification")
        for member, digest in expected.items():
            if hashlib.sha256(bundle.read(member)).hexdigest() != digest:
                raise ValueError("Archive file does not match reviewed source: " + member)
    temporary.replace(archive)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")
    print(f"Verified {len(expected)} files: {archive.relative_to(ROOT).as_posix()}")
    print(f"Size: {archive.stat().st_size:,} bytes; SHA-256: {checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
