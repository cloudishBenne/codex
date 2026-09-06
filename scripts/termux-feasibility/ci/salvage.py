#!/usr/bin/env python3
"""Independent salvage of known producer outputs, usable even if driver crashed."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def digest(path):
    with path.open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()


def capture(root, dest, include_logs=True):
    root, dest = Path(root).resolve(), Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    index = dest / 'salvage.json'
    previous = json.loads(index.read_text()).get('files', {}) if index.exists() else {}
    files, errors = dict(previous), []
    targets = []
    target = root / 'producer-target'
    if target.exists():
        for pattern in ['librusty_v8.a', 'src_binding*.rs', 'args.gn', 'project.json', 'build.ninja']:
            targets.extend(target.rglob(pattern))
    if include_logs:
        for folder in ['evidence', 'logs']:
            d = root / folder
            if d.exists(): targets.extend(p for p in d.rglob('*') if p.is_file())
    for p in sorted(set(targets)):
        try:
            if not p.is_file() or not p.resolve().is_relative_to(root): continue
            rel = str(p.relative_to(root)); st = p.stat(); stamp = [st.st_size, st.st_mtime_ns]
            if files.get(rel, {}).get('stamp') == stamp and (dest / rel).exists(): continue
            q = dest / rel; q.parent.mkdir(parents=True, exist_ok=True)
            tmp = q.with_name(q.name + '.part')
            shutil.copy2(p, tmp)
            before = stamp; after = p.stat()
            stable = before == [after.st_size, after.st_mtime_ns]
            h = digest(tmp); tmp.replace(q)
            item = {'size': q.stat().st_size, 'sha256': h, 'stamp': stamp, 'stable_during_copy': stable}
            if p.name == 'librusty_v8.a':
                ar = root / 'tools/clang/bin/llvm-ar'
                tool = str(ar) if ar.is_file() else shutil.which('ar')
                if tool:
                    inv = subprocess.run([tool, 't', str(q)], capture_output=True, timeout=120)
                    member = q.with_name(q.name + '.members.txt'); member.write_bytes(inv.stdout + inv.stderr)
                    item.update(member_exit=inv.returncode, member_inventory=str(member.relative_to(dest)), member_sha256=digest(member))
                    if (root / 'evidence').is_dir(): shutil.copy2(member, root / 'evidence/native-archive-members.txt')
                else: item['member_error'] = 'No archive inventory tool available'
            files[rel] = item
            if p.name == 'librusty_v8.a' or p.name.startswith('src_binding'):
                print('RR_SALVAGE ' + json.dumps({'file': rel, **item}), flush=True)
        except Exception as exc:
            errors.append({'file': str(p), 'error': str(exc)})
    expected = 'producer-target/aarch64-linux-android/release/gn_out/'
    archive = files.get(expected + 'obj/librusty_v8.a')
    binding = files.get(expected + 'src_binding.rs')
    label = 'NO_CAPTURED_PAIR'
    if archive: label = 'PARTIAL_NATIVE_ARCHIVE_ONLY'
    if archive and binding: label = 'PARTIAL_PAIR_CONSUMER_NOT_VALIDATED'
    consumer = root / 'evidence/consumer-result.json'
    if archive and binding and consumer.exists():
        c = json.loads(consumer.read_text())
        label = 'PARTIAL_PAIR_CONSUMER_FAILED' if c.get('result') != 'PASS' else 'PAIR_AND_CONSUMER_AWAITING_REVIEW'
    data = {'label': label, 'captured_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'files': files, 'errors': errors}
    temp = index.with_suffix('.tmp'); temp.write_text(json.dumps(data, indent=2) + '\n'); temp.replace(index)
    if (root / 'evidence').is_dir():
        name = 'driver-salvage.json' if dest.name == 'salvage-live' else 'workflow-salvage.json'
        (root / 'evidence' / name).write_text(json.dumps(data, indent=2) + '\n')
    return data


if __name__ == '__main__':
    result = capture(sys.argv[1], sys.argv[2])
    print('RR_SALVAGE_RESULT ' + json.dumps({'label': result['label'], 'files': len(result['files']), 'errors': result['errors']}), flush=True)
    # A failed capture remains visible, but the separate upload still runs.
    sys.exit(1 if result['errors'] else 0)
