"""Pinned host-only libclang and exact locked bindgen smoke. No V8 compilation."""
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tomllib

VERSION = '1:19.1.1-1ubuntu1~24.04.2'
PACKAGES = [
    ('libclang-common-19-dev', 'universe', '951e803e3540e2d1513f44fad07d179a85dc82b8cbb060f09ae3c602e47a8695'),
    ('libclang1-19', 'universe', 'ca0340e157ab40939d40b88eb17bb14532854df631bd6fa7c6fe538401d2080e'),
    ('libllvm19', 'main', 'a62f1074eb7aecf10e00fed9c7c967dc6c801fae444a40b8007e246320f5ee15'),
]

def prepare(root, evidence, source, env, run, download, sha):
    dest = root / 'tools/host-libclang'
    dest.mkdir(parents=True, exist_ok=True)
    for package, section, digest in PACKAGES:
        name = f'{package}_19.1.1-1ubuntu1~24.04.2_amd64.deb'
        url = f'https://archive.ubuntu.com/ubuntu/pool/{section}/l/llvm-toolchain-19/{name}'
        deb = download(url, name, digest)
        metadata = run(['dpkg-deb', '-f', deb, 'Package', 'Version', 'Architecture'])
        assert package in metadata and VERSION in metadata and 'amd64' in metadata
        run(['dpkg-deb', '-x', deb, dest])
    libdir = dest / 'usr/lib/x86_64-linux-gnu'
    matches = list(libdir.glob('libclang-19.so*'))
    paths = {p.resolve() for p in matches if p.is_file()}
    assert len(paths) == 1, f'Ambiguous libclang: {paths}'
    lib = paths.pop()
    assert lib.is_relative_to(dest)
    assert sha(lib) == 'c88d72e7a720188e4da36feee7605ddd539170d4cce9871f51e0fb48cfc10760'
    effective = dict(env)
    # Scoped to producer/consumer process trees, not exported to global shell.
    # clang-sys accepts a file path and therefore cannot select another candidate.
    effective['LIBCLANG_PATH'] = str(lib)
    effective['LD_LIBRARY_PATH'] = str(libdir)
    identity = {'source': 'Ubuntu noble-updates llvm-toolchain-19', 'package_version': VERSION,
                'packages': PACKAGES, 'path': str(lib), 'size': lib.stat().st_size,
                'sha256': sha(lib), 'environment': {k: effective[k] for k in ['LIBCLANG_PATH', 'LD_LIBRARY_PATH']}}
    resource = dest / 'usr/lib/llvm-19/lib/clang/19'
    assert (resource / 'include/stddef.h').is_file() and (resource / 'include/stdint.h').is_file()
    identity['resource_directory'] = str(resource)
    effective['RR_LIBCLANG_RESOURCE'] = str(resource)
    identity['environment']['RR_LIBCLANG_RESOURCE'] = str(resource)
    identity['file'] = run(['file', '-L', lib], env=effective)
    assert 'ELF 64-bit' in identity['file'] and 'x86-64' in identity['file']
    identity['dynamic'] = run(['readelf', '-d', lib], env=effective)
    identity['ldd'] = run(['ldd', lib], env=effective)
    assert 'not found' not in identity['ldd']
    companions = re.findall(r'=> (/\S+)', identity['ldd'])
    identity['dependencies'] = {p: sha(p) for p in companions}
    code = '''import ctypes,json,sys
class CXString(ctypes.Structure):
    _fields_=[('data',ctypes.c_void_p),('flags',ctypes.c_uint)]
p=sys.argv[1]; c=ctypes.CDLL(p)
c.clang_getClangVersion.restype=CXString
c.clang_getCString.argtypes=[CXString];c.clang_getCString.restype=ctypes.c_char_p
c.clang_disposeString.argtypes=[CXString]
s=c.clang_getClangVersion(); v=c.clang_getCString(s).decode(); c.clang_disposeString(s)
assert '19.1.1' in v,v
print(json.dumps({'loaded':p,'version':v,'status':'PASS'}))
'''
    identity['direct_load'] = json.loads(run([sys.executable, '-c', code, lib], env=effective))
    identity['result'] = 'LOADER_PASS_SMOKE_PENDING'
    (evidence / 'libclang-preflight.json').write_text(json.dumps(identity, indent=2) + '\n')
    smoke = root / 'bindgen-smoke'
    smoke.mkdir()
    (smoke / 'src').mkdir()
    (smoke / 'Cargo.toml').write_text('[package]\nname="rr-bindgen-smoke"\nversion="0.0.0"\nedition="2021"\n[dependencies]\nbindgen="=0.72.0"\n')
    # Derive the smoke lock exclusively from the accepted producer lock graph.
    text = (source / 'Cargo.lock').read_text()
    packages = tomllib.loads(text)['package']
    selected = set()
    def visit(name, version=None):
        found = [p for p in packages if p['name'] == name and (version is None or p['version'] == version)]
        assert len(found) == 1, (name, version)
        p = found[0]; key = (p['name'], p['version'])
        if key in selected: return
        selected.add(key)
        for dep in p.get('dependencies', []):
            fields = dep.split(); visit(fields[0], fields[1] if len(fields) > 1 else None)
    visit('bindgen', '0.72.0')
    chunks = []
    for block in text.split('[[package]]')[1:]:
        p = tomllib.loads('[[package]]' + block)['package'][0]
        if (p['name'], p['version']) in selected: chunks.append('[[package]]' + block)
    (smoke / 'Cargo.lock').write_text('version = 4\n\n' + ''.join(chunks) + '\n[[package]]\nname = "rr-bindgen-smoke"\nversion = "0.0.0"\ndependencies = ["bindgen"]\n')
    (smoke / 'src/main.rs').write_text('''fn main() {
 let args: Vec<String> = std::env::args().collect();
 let v = bindgen::clang_version();
 println!("libclang version: {}", v.full);
 assert!(v.parsed.unwrap().0 >= 19);
 let mut builder = bindgen::Builder::default()
  .clang_arg(format!("-resource-dir={}", std::env::var("RR_LIBCLANG_RESOURCE").unwrap()))
  .header_contents("rr_smoke.h", "#include <stddef.h>\n#include <stdint.h>\ntypedef struct { int x; uint64_t y; size_t n; } rr_point; int rr_add(int a, int b);");
 if args.get(2).map(String::as_str) == Some("android") {
  builder = builder.clang_arg("--target=aarch64-linux-android29")
   .clang_arg(format!("--sysroot={}", std::env::var("RR_ANDROID_SYSROOT").unwrap()));
 }
 let b = builder.generate().expect("bindgen 0.72.0 smoke");
 let s = b.to_string(); assert!(s.contains("rr_point") && s.contains("rr_add"));
 b.write_to_file(&args[1]).unwrap();
 let maps = std::fs::read_to_string("/proc/self/maps").unwrap();
 let exact = std::env::var("LIBCLANG_PATH").unwrap();
 assert!(maps.contains(&exact), "selected libclang not mapped");
 println!("BINDGEN_0_72_0_SMOKE_PASS loaded={}", exact);
}''')
    smoke_env = dict(effective, CARGO_TARGET_DIR=str(root / 'smoke-target'))
    # The smoke is a host executable. Remove target selection, retain exact loader env.
    smoke_env.pop('CARGO_BUILD_TARGET', None)
    before = sha(smoke / 'Cargo.lock')
    for name in ['Cargo.toml', 'Cargo.lock']:
        shutil.copy2(smoke / name, evidence / ('smoke-' + name))
    shutil.copy2(smoke / 'src/main.rs', evidence / 'smoke-main.rs')
    result = run(['rustup', 'run', '1.91.0', 'cargo', 'run', '--locked', '--manifest-path', smoke / 'Cargo.toml', '--', smoke / 'out.rs'], env=smoke_env)
    assert 'BINDGEN_0_72_0_SMOKE_PASS' in result and sha(smoke / 'Cargo.lock') == before
    android = run(['rustup', 'run', '1.91.0', 'cargo', 'run', '--locked', '--manifest-path', smoke / 'Cargo.toml', '--', smoke / 'android-out.rs', 'android'], env=smoke_env)
    assert 'BINDGEN_0_72_0_SMOKE_PASS' in android and sha(smoke / 'Cargo.lock') == before
    identity['smoke'] = {'bindgen': '0.72.0', 'lock_sha256': before, 'bindings_sha256': sha(smoke / 'out.rs'), 'android_bindings_sha256': sha(smoke / 'android-out.rs'), 'host': 'PASS', 'android_api29': 'PASS', 'result': 'PASS'}
    for name in ['Cargo.toml', 'Cargo.lock', 'out.rs', 'android-out.rs']:
        shutil.copy2(smoke / name, evidence / ('smoke-' + name))
    shutil.copy2(smoke / 'src/main.rs', evidence / 'smoke-main.rs')
    identity['result'] = 'PASS'
    (evidence / 'libclang-preflight.json').write_text(json.dumps(identity, indent=2) + '\n')
    return effective, identity
