"""Audit/export exactly Git's unignored files without initializing Git in the project."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, help="New empty directory for a clean public copy")
    args = parser.parse_args()
    # A separate temporary Git database evaluates the real .gitignore; project Git state is untouched.
    with tempfile.TemporaryDirectory(prefix="ko-public-") as temp:
        metadata = Path(temp)/"audit.git"
        subprocess.run(["git", "init", "--bare", "--quiet", str(metadata)], check=True)
        result = subprocess.run(["git", "--git-dir="+str(metadata), "--work-tree="+str(ROOT),
                                 "ls-files", "--others", "--exclude-standard", "-z"],
                                check=True, capture_output=True)
        files = sorted(name for name in result.stdout.decode("utf-8").split("\0") if name)
    errors = []
    patterns = [r"\bsk-[A-Za-z0-9_-]{20,}\b", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
                r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"]
    public = set(files)
    for name in files:
        path = ROOT/name
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pem", ".key"} or path.name == "snapshot.json":
            errors.append("Excluded artifact unexpectedly included: " + name)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            continue
        if any(re.search(pattern, text) for pattern in patterns):
            errors.append("Credential-like match (value redacted): " + name)
        if path.suffix == ".py":
            ast.parse(text, filename=name)
        if path.suffix == ".md":
            # Markdown links only; command examples and intentionally literal code are not parsed.
            for target in re.findall(r"!?\[[^]\n]*\]\(([^)]+)\)", text):
                target = target.strip().strip("<>")
                parts = urlsplit(target)
                if parts.scheme or not parts.path:
                    continue
                absolute = (path.parent/unquote(parts.path)).resolve()
                try:
                    relative = absolute.relative_to(ROOT).as_posix()
                except ValueError:
                    errors.append("Link outside public repository: " + name + " -> " + target)
                    continue
                if not absolute.exists() or (absolute.is_file() and relative not in public):
                    errors.append("Unavailable public link: " + name + " -> " + target)
    if errors:
        raise SystemExit("\n".join(errors))
    report = {"files": len(files), "bytes": sum((ROOT/name).stat().st_size for name in files),
              "credential_pattern_matches": 0, "excluded_artifacts": 0,
              "note": "Pattern scan is a heuristic, not a complete secrets audit."}
    (ROOT/"data").mkdir(exist_ok=True)
    manifest = {"audit": report, "files": [{"path": name, "sha256": hashlib.sha256((ROOT/name).read_bytes()).hexdigest()} for name in files]}
    (ROOT/"data/public-file-manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    if args.export:
        destination = args.export.resolve()
        if destination == ROOT or ROOT in destination.parents:
            parser.error("Export outside the project to avoid recursively including its own copy")
        if destination.exists() and any(destination.iterdir()):
            parser.error("Export destination must be a new or empty directory")
        destination.mkdir(parents=True,exist_ok=True)
        for name in files:
            target = destination/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(ROOT/name,target)
        report["export"] = str(destination)
    print(json.dumps(report,indent=2))

if __name__ == "__main__":
    main()
