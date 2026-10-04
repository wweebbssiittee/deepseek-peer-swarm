"""Check publication candidates without printing matching secret values.

Uses tracked plus non-ignored untracked files in Git, or a source-tree scan
before Git initialization. This is a local heuristic, not a complete secret or
personal-data audit. Images still require visual review; history needs its own
scan if this project is imported into an existing repository.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
LOCAL_ONLY = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".browser-check", "build", "dist"}
PRIVATE_DIRS = {"workspaces", ".swarm-backups", "runtime", "exports", "private", "data", "scratch"}
SOURCE_DIRS = {".github", "docs", "scripts", "swarm", "tests"}
PRIVATE_NAMES = {"access-token.txt", "settings.json", "notifications.json", "server.lock", "id_rsa", "id_ed25519"}
PRIVATE_SUFFIX = re.compile(r"\.(?:sqlite\d*|db)(?:[-.].*)?$|\.(?:enc(?:\.tmp)?|pem|key|log)$", re.I)
PATTERNS = {
    "possible API key": re.compile(r"\bsk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{32,}\b"),
    "possible GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "possible Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "possible AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private key material": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "personal home path": re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/(?:home|Users)/)[^\s\\/<>]+"),
}
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def candidates(root: Path) -> list[Path]:
    # Do not inherit a parent repository: a new project must scan itself fully.
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    except FileNotFoundError:
        result = None  # A downloaded source ZIP does not require Git.
    if result is not None and result.returncode == 0 and Path(result.stdout.strip()).resolve() == root.resolve():
        listing = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"], capture_output=True, check=True)
        return sorted({root / os.fsdecode(name) for name in listing.stdout.split(b"\0") if name})
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in list(dirs):
            if (parent / name).is_symlink():
                files.append(parent / name)
                dirs.remove(name)
            elif name in LOCAL_ONLY or name.endswith(".egg-info"):
                dirs.remove(name)
        files.extend(parent / name for name in names)
    return sorted(files)


def scan_file(root: Path, path: Path) -> list[str]:
    relative = path.relative_to(root)
    label = relative.as_posix()
    findings = []
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        return [f"{label}: symlink or outside-tree path requires review"]
    if not path.exists():  # A tracked deletion is not a publication candidate.
        return []
    if (any(part in PRIVATE_DIRS or part in LOCAL_ONLY for part in relative.parts)
            or path.name in PRIVATE_NAMES
            or (path.name.startswith(".env") and path.name != ".env.example")
            or PRIVATE_SUFFIX.search(path.name)):
        return [f"{label}: private runtime/generated file"]
    if len(relative.parts) > 1 and relative.parts[0] not in SOURCE_DIRS:
        return [f"{label}: unexpected top-level folder requires publication review"]
    if path.stat().st_size > 2_000_000:
        return [f"{label}: large file requires manual publication review"]
    data = path.read_bytes()
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".ico"}:
        return []  # Text patterns cannot establish that a screenshot is safe.
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [f"{label}: unrecognized binary file requires review"]
    for number, line in enumerate(content.splitlines(), 1):
        for kind, pattern in PATTERNS.items():
            if pattern.search(line):
                findings.append(f"{label}:{number}: {kind}")
        for match in EMAIL.finditer(line):
            domain = match.group(1).lower()
            if not (domain in {"example.com", "example.org", "example.net"}
                    or domain.endswith((".example", ".invalid", ".test", ".localhost"))):
                findings.append(f"{label}:{number}: email address requires review")
    return findings


def main() -> int:
    files = candidates(ROOT)
    findings = [item for path in files for item in scan_file(ROOT, path)]
    if findings:
        print("Publication check failed (values withheld):")
        print("\n".join(findings))
        return 1
    images = sum(path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".ico"} for path in files)
    print(f"Publication check passed: {len(files)} files; {images} images require visual review. Git history is not scanned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
