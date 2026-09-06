"""Tee subprocess bytes, retain live structured state and emit resource heartbeats."""
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time
import shutil
from salvage import capture


class Observer:
    def __init__(self, root, manifest, save, start, seconds=10200, interval=60):
        self.root, self.m, self.save, self.start = Path(root), manifest, save, start
        self.deadline, self.interval = start + seconds, interval
        self.latest, self.ninja = '', None
        self.phase, self.warning_count = manifest['stage'], 0
        self.events = self.root / 'evidence/events.jsonl'
        self.consumer = False

    def emit(self, kind, **fields):
        event = {'kind': kind, 'stage': self.phase, 'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'elapsed_seconds': round(time.time()-self.start, 1), **fields}
        print('RR_EVENT ' + json.dumps(event), flush=True)
        with self.events.open('a') as f: f.write(json.dumps(event) + '\n')
        p = self.root / 'evidence/live-state.json'; tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(event, indent=2) + '\n'); tmp.replace(p)

    def stage(self, phase, **fields):
        self.phase = phase; self.emit('stage', **fields)

    def heartbeat(self, proc):
        mem = {k: int(v) for k, v in re.findall(r'^(MemTotal|MemAvailable):\s+(\d+)', Path('/proc/meminfo').read_text(), re.M)}
        cpu = Path('/proc/stat').read_text().splitlines()[0].split()[1:]
        process = {'pid': proc.pid, 'alive': proc.poll() is None}
        try:
            stat = Path(f'/proc/{proc.pid}/stat').read_text().rsplit(')', 1)[1].split()
            process.update(state=stat[0], cpu_user_ticks=int(stat[11]), cpu_system_ticks=int(stat[12]))
        except (OSError, ValueError): pass
        self.emit('heartbeat', last_ninja=self.ninja, last_line=self.latest[-600:], load=os.getloadavg(), cpu_ticks=cpu, memory_kib=mem, disk_free_bytes=shutil.disk_usage(self.root).free, process=process, jobserver_warnings=self.warning_count)

    def line(self, line):
        line = line.strip()
        if not line: return
        self.latest = line[-600:]
        if 'failed to connect to jobserver' in line: self.warning_count += 1
        marker = re.search(r'\[(\d+)/(\d+)\]\s+(.+)', line)
        if marker: self.ninja = marker.group(0)[-400:]
        if 'RR_STAGE ' in line:
            value = line.split('RR_STAGE ', 1)[1].strip()
            self.stage(value)
            if value in ['archive observed', 'bindings observed']:
                result = capture(self.root, self.root / 'salvage-live', include_logs=False)
                self.emit('capture', label=result['label'], errors=result['errors'])
                if result['errors']: raise RuntimeError('Immediate output capture failed')
        elif 'gn gen --root=' in line: self.stage('GN setup')
        elif marker and not self.consumer and self.phase in ['GN setup', 'native build start']:
            self.stage('native compilation')

    def run(self, args, cwd=None, env=None, allowed=(0,)):
        label = f"{len(self.m['commands']):03d}"
        record = {'argv': [str(a) for a in args], 'cwd': str(cwd or self.root), 'stage': self.m['stage'], 'log': f'logs/{label}.log', 'exit': None, 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        self.m['commands'].append(record); self.save()
        self.emit('command_start', **record)
        remaining = self.deadline - time.time()
        if remaining <= 0: raise RuntimeError('Current continuation run time budget exhausted')
        proc = subprocess.Popen(record['argv'], cwd=cwd or self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        sel = selectors.DefaultSelector(); sel.register(proc.stdout, selectors.EVENT_READ)
        next_beat = time.monotonic() + self.interval; pending = b''
        try:
            with (self.root / record['log']).open('wb') as log:
                while sel.get_map():
                    if time.time() >= self.deadline: raise TimeoutError('Current continuation run time budget exhausted')
                    for key, _ in sel.select(timeout=1):
                        chunk = os.read(key.fileobj.fileno(), 16384)
                        if not chunk:
                            sel.unregister(key.fileobj); continue
                        log.write(chunk); log.flush()
                        sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()
                        pending += chunk
                        pieces = re.split(b'[\r\n]', pending); pending = pieces.pop()
                        for piece in pieces: self.line(piece.decode(errors='replace'))
                    if time.monotonic() >= next_beat:
                        self.heartbeat(proc); next_beat = time.monotonic() + self.interval
                if pending: self.line(pending.decode(errors='replace'))
                record['exit'] = proc.wait(timeout=max(1, self.deadline - time.time()))
        except BaseException:
            try: os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                proc.wait()
            record['exit'] = proc.returncode
            raise
        finally:
            sel.close(); proc.stdout.close()
            record['ended_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            self.m['jobserver_warnings'] = self.warning_count; self.save()
            self.emit('command_end', log=record['log'], exit=record['exit'])
        if record['exit'] not in allowed: raise RuntimeError(f"command {label} failed: {record['exit']}")
        return (self.root / record['log']).read_text(errors='replace')
