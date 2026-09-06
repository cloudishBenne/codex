#!/usr/bin/env python3
"""Independent diagnostic salvage; files here do not imply a successful build."""

import hashlib
import json
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve()
dest = Path(sys.argv[2]).resolve()
dest.mkdir(parents=True, exist_ok=False)
rows = []
errors = []
if root.is_dir():
    candidates = set()
    for folder in ["evidence", "logs", "artifacts", "pair"]:
        p = root / folder
        if p.is_dir():
            candidates.update(
                x for x in p.rglob("*") if x.is_file() and not x.is_symlink()
            )
    for name in ["codex", "codex-code-mode-host"]:
        p = root / "target/aarch64-linux-android/release" / name
        if p.is_file():
            candidates.add(p)
    for p in sorted(candidates):
        try:
            relative = p.relative_to(root)
            out = dest / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)
            with out.open("rb") as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            rows.append(
                {"path": str(relative), "bytes": out.stat().st_size, "sha256": digest}
            )
        except Exception as exc:
            errors.append({"path": str(p), "error": str(exc)})
(dest / "salvage-manifest.json").write_text(
    json.dumps(
        {
            "status": "DIAGNOSTIC_SALVAGE_NOT_BUILD_PASS",
            "files": rows,
            "errors": errors,
        },
        indent=2,
    )
    + "\n"
)
print("Independent salvage:", len(rows), "files;", len(errors), "errors", flush=True)
if errors:
    sys.exit(1)
