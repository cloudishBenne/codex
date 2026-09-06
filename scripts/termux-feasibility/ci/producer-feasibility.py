#!/usr/bin/env python3
"""Bounded PART A only. A successful run still requires Development evidence review."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.parse
import urllib.request
import zipfile
from observe import Observer
from salvage import capture
from preflight import prepare as libclang_preflight

BASE = "3d2ee51ca2d5db578f328aa75e20aa22c0197c9a"
V8 = "5c15a6995c9bb4bacd3e341b59fff32c909c80bf"
CRATE = "42a978ff11f15b24e5c05a7123cf2b68f41e763546699781a924ef4e2cf43a49"
REV = "llvmorg-23-init-10931-g20b6ec66-11"
ROOT = Path(os.environ["RR_ROOT"]).resolve()
KIT = Path(__file__).resolve().parent.parent
E = ROOT / "evidence"
A = ROOT / "artifacts"
S = ROOT / "rusty-v8-src"
C = ROOT / "codex-src"
START = time.time()
MODE = os.environ.get("RR_MODE", "preflight")
assert MODE in ["preflight", "producer"]
M = {"schema_version": 1, "result": "NOT_EVALUATED", "stage": "A0",
     "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
     "budget_minutes": 170, "max_correction_runs": 1, "correction_run": 0, "continuation": "control-v0.2", "mode": MODE,
     "prior_run": 34015167037, "run_budget_seconds": 1800 if MODE == "preflight" else 10200, "part_b_entered": False,
     "commands": [], "downloads": [], "patches": [], "outputs": {},
     "workflow": {k: os.getenv(k) for k in ["GITHUB_SHA", "GITHUB_REF", "GITHUB_WORKFLOW_REF",
         "GITHUB_WORKFLOW_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "ImageOS", "ImageVersion"]}}


def sha(p):
    with open(p, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def save():
    (E / "feasibility-result.json").write_text(json.dumps(M, indent=2) + "\n")


OBSERVER = Observer(ROOT, M, save, START, seconds=1800 if MODE == 'preflight' else 10200)


def run(args, cwd=None, env=None, allowed=(0,)):
    return OBSERVER.run(args, cwd, env, allowed)


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def download(url, name, expected=None):
    p = ROOT / "downloads" / name
    with urllib.request.urlopen(url, timeout=120) as r, p.open("wb") as f:
        shutil.copyfileobj(r, f)
    digest = sha(p)
    M["downloads"].append({"source": urllib.parse.urlunsplit(urllib.parse.urlsplit(url)._replace(query="", fragment="")),
                           "file": name, "sha256": digest, "bytes": p.stat().st_size})
    save()
    require(expected is None or digest == expected, f"download checksum mismatch: {name}")
    return p


def unpack(p, dest):
    dest.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as z:
            for i in z.infolist():
                target = dest / i.filename
                require(target.resolve().is_relative_to(dest.resolve()), "ZIP path escape")
                mode = i.external_attr >> 16
                if i.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                elif stat.S_ISLNK(mode):
                    link = z.read(i).decode()
                    require((target.parent / link).resolve().is_relative_to(dest.resolve()), "ZIP link escape")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(link)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(i) as src, target.open("wb") as out:
                        shutil.copyfileobj(src, out)
                    target.chmod((mode & 0o777) or 0o644)
    else:
        with tarfile.open(p) as t:
            t.extractall(dest, filter="data")


def cipd(package, version, name, dest, expected_instance=None):
    api = "https://chrome-infra-packages.appspot.com/_ah/api/repo/v1/"
    def get(endpoint, params):
        with urllib.request.urlopen(api + endpoint + "?" + urllib.parse.urlencode(params), timeout=120) as r:
            return json.load(r)
    resolved = get("instance/resolve", {"package_name": package, "version": version})
    instance = resolved["instance_id"]
    require(expected_instance is None or instance == expected_instance, f"CIPD drift: {name}")
    info = get("instance", {"package_name": package, "instance_id": instance})
    p = download(info["fetch_url"], name + ".zip")
    M["downloads"][-1].update(package=package, version=version, instance_id=instance)
    save()
    unpack(p, dest)


def tracked(repo):
    # Content, mode and path identity of all tracked files, including submodules.
    data = subprocess.check_output(["git", "ls-files", "--recurse-submodules", "-z"], cwd=repo)
    h = hashlib.sha256()
    for name in sorted(data.split(b"\0")):
        if not name:
            continue
        p = repo / os.fsdecode(name)
        h.update(name + b"\0" + str(p.lstat().st_mode).encode() + b"\0")
        h.update(os.readlink(p).encode() if p.is_symlink() else bytes.fromhex(sha(p)))
    return h.hexdigest()


def main():
    require(platform.machine() == "x86_64" and sys.platform == "linux", "requires x86_64 Linux")
    M["host"] = {"uname": list(platform.uname()), "cpu_count": os.cpu_count(),
                 "disk": shutil.disk_usage(ROOT)._asdict(), "meminfo": Path("/proc/meminfo").read_text()}
    require(shutil.disk_usage(ROOT).free > 18 * 1024**3, "less than 18 GiB free before source/build setup")
    for tool in ["git", "rustup", "cmake", "make", "pkg-config", "cc", "c++"]:
        require(shutil.which(tool), f"missing host prerequisite: {tool}")
        run([tool, "--version"])
    M["stage"] = "A1"
    for url, tag, commit, dest in [("https://github.com/denoland/rusty_v8.git", "v150.4.0", V8, S),
                                   ("https://github.com/openai/codex.git", "rust-v0.153.4", BASE, C)]:
        run(["git", "clone", "--depth", "1", "--branch", tag, "--single-branch", url, dest])
        require(run(["git", "rev-parse", "HEAD"], dest).strip() == commit, "source SHA mismatch")
        run(["git", "submodule", "update", "--init", "--recursive", "--depth", "1", "--jobs", "4"], dest)
        snapshot = run(["git", "submodule", "status", "--recursive"], dest)
        require(all(x.startswith(" ") for x in snapshot.splitlines()), "submodule mismatch")
    require(run(["git", "rev-parse", "refs/tags/rust-v0.153.4^{tag}"], C).strip() ==
            "042fb41b7c813ac7999105e886b2b7aa715b5081", "Codex tag object mismatch")
    pins = {"v8": "ac1e23989121713ca642f6650b34deff7b686896", "build": "8acb33ac8dceef0503443109c0a92988189563ef",
            "buildtools": "17495e454aae81b581e8b3caccbb53054509b280", "tools/clang": "45f4b9e25124809497a27a8ae0e63d603b0f9f1b",
            "third_party/libc++/src": "5abc7f839700f0f17338434e1c1c6a8c87c00c11", "third_party/libc++abi/src": "8f11bb1d4438d0239d0dfc1bd9456a9f31629dda",
            "third_party/rust": "26e8ff47f18a8d28d6187a04b6a16cb7332356f8"}
    for path, commit in pins.items():
        require(run(["git", "rev-parse", "HEAD"], S / path).strip() == commit, f"gitlink mismatch: {path}")
    lock = C / "codex-rs/Cargo.lock"
    v8 = [x for x in tomllib.loads(lock.read_text())["package"] if x["name"] == "v8"]
    require(len(v8) == 1 and v8[0]["version"] == "150.4.0" and v8[0]["checksum"] == CRATE, "V8 lock mismatch")
    M["sources"] = {"codex": BASE, "rusty_v8": V8, "critical_gitlinks": pins,
                    "codex_lock": sha(lock), "producer_lock": sha(S / "Cargo.lock")}
    shutil.copy2(S / "v8/DEPS", E / "v8-DEPS")
    shutil.copy2(S / "tools/clang/scripts/update.py", E / "clang-update.py")
    download("https://static.crates.io/crates/v8/v8-150.4.0.crate", "v8-150.4.0.crate", CRATE)
    M["stage"] = "A2"
    run(["rustup", "toolchain", "install", "1.91.0", "--profile", "minimal", "--target", "aarch64-linux-android"])
    run(["rustup", "toolchain", "install", "1.95.0", "--profile", "minimal", "--target", "aarch64-linux-android"])
    for version in ["1.91.0", "1.95.0"]:
        run(["rustup", "run", version, "rustc", "-Vv"])
        run(["rustup", "run", version, "cargo", "-V"])
    cipd("gn/gn/linux-amd64", "git_revision:3357c4f51b1a9e676378c695dd9c7e9911c35ee6", "gn", ROOT / "tools/gn")
    cipd("infra/3pp/tools/ninja/linux-amd64", "version:3@1.12.1.chromium.4", "ninja", ROOT / "tools/ninja")
    ndk = S / "third_party/android_toolchain/ndk"
    # A3 explicitly permits the known r26c integration after the exact DEPS trial.
    # Run 34014758687 proved the stripped DEPS package lacks Android libunwind.a.
    ndk_zip = download("https://dl.google.com/android/repository/android-ndk-r26c-linux.zip", "android-ndk-r26c-linux.zip")
    with ndk_zip.open("rb") as f:
        ndk_sha1 = hashlib.file_digest(f, "sha1").hexdigest()
    require(ndk_zip.stat().st_size == 668556021 and ndk_sha1 == "7faebe2ebd3590518f326c82992603170f07c96e", "official r26c package identity mismatch")
    M["downloads"][-1].update(version="26.2.11394342", sha1=ndk_sha1,
        checksum_source="https://github.com/android/ndk/wiki/Home/90bb494b13366920b0e05807a6933af5a59926dc")
    M["ndk_selection"] = {"previous": "2@30.0.14608247", "selected": "26.2.11394342",
        "reason": "Missing complete consumer compiler/runtime in DEPS NDK; A3 r26c integration",
        "scope": "single NDK sysroot for V8, bindgen and consumer; NDK consumer tools; unchanged Chromium V8 compiler"}
    save()
    unpack(ndk_zip, ROOT / "tools/ndk-unpack")
    require(not ndk.exists(), "NDK destination already exists")
    ndk.parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "tools/ndk-unpack/android-ndk-r26c").rename(ndk)
    require("Pkg.Revision = 26.2.11394342" in (ndk / "source.properties").read_text(), "NDK package revision mismatch")
    for package in ["clang"]:
        url = f"https://commondatastorage.googleapis.com/chromium-browser-clang/Linux_x64/{package}-{REV}.tar.xz"
        unpack(download(url, package + ".tar.xz"), ROOT / "tools/clang")
    rusturl = "https://storage.googleapis.com/chromium-browser-clang/Linux_x64/rust-toolchain-4c4205163abcbd08948b3efab796c543ba1ea687-4-llvmorg-23-init-10931-g20b6ec66.tar.xz"
    unpack(download(rusturl, "chromium-rust.tar.xz", "832de79f8d90940f4aaef023f83a00c1e7210c023f4d57f606b7bf9831c889aa"), S / "third_party/rust-toolchain")
    (S / "third_party/rust-toolchain/.rusty_v8_version").write_text(rusturl)
    sysroots = json.loads((S / "build/linux/sysroot_scripts/sysroots.json").read_text())["bullseye_amd64"]
    require(sysroots["Sha256Sum"] == "52d61d4446ffebfaa3dda2cd02da4ab4876ff237853f46d273e7f9b666652e1d", "sysroot drift")
    unpack(download(sysroots["URL"] + "/" + sysroots["Sha256Sum"], "host-sysroot.tar.xz", sysroots["Sha256Sum"]), S / "build/linux" / sysroots["SysrootDir"])
    for path, url, commit in [("android_platform", "https://chromium.googlesource.com/chromium/src/third_party/android_platform.git", "e3919359f2387399042d31401817db4a02d756ec"),
                              ("catapult", "https://chromium.googlesource.com/catapult.git", "2852bb7e91e4995502ffb72b7ed21412ee157914")]:
        d = S / "third_party" / path
        d.mkdir()
        run(["git", "init"], d)
        run(["git", "fetch", "--depth", "1", url, commit], d)
        run(["git", "checkout", "--detach", "FETCH_HEAD"], d)
        require(run(["git", "rev-parse", "HEAD"], d).strip() == commit, "extra source mismatch")
    M["stage"] = "A3"
    for patch, repo in [("0001-final-android-bindgen-and-prepared-ndk.patch", S), ("0002-android-ndk-version-input.patch", S / "build"), ("0003-stage-observation.patch", S), ("0004-final-bindgen-resource-directory.patch", S)]:
        p = KIT / "patches" / patch
        changed_paths = [line[6:] for line in p.read_text().splitlines() if line.startswith("+++ b/")]
        before_files = {name: sha(repo / name) for name in changed_paths}
        run(["git", "apply", "--unidiff-zero", "--check", p], repo)
        run(["git", "apply", "--unidiff-zero", p], repo)
        M["patches"].append({"file": patch, "sha256": sha(p), "repo": str(repo), "before": before_files, "after": {name: sha(repo / name) for name in changed_paths}})
        (E / patch).write_bytes(p.read_bytes())
        run(["git", "diff", "--check"], repo)
        run(["git", "diff"], repo)
    (S / "third_party/android_ndk").symlink_to("android_toolchain/ndk", target_is_directory=True)
    tc = ndk / "toolchains/llvm/prebuilt/linux-x86_64"
    require((tc / "sysroot/usr/include/stdio.h").is_file(), "NDK sysroot layout unresolved")
    # The complete selected NDK supplies consumer compiler, runtime and sysroot.
    # V8 and final bindgen still use the pinned Chromium compiler and verified Ubuntu host libclang.
    for name, directory in [('ndk', ndk), ('clang', ROOT / 'tools/clang')]:
        (E / (name + '-inventory.txt')).write_text('\n'.join(
            str(p.relative_to(directory)) + (' -> ' + os.readlink(p) if p.is_symlink() else '')
            for p in sorted(directory.rglob('*')) if p.is_file() or p.is_symlink()) + '\n')
    wrappers = ROOT / 'tools/android-wrappers'
    wrappers.mkdir()
    for name, executable in [('cc', 'clang'), ('cxx', 'clang++')]:
        compiler = tc / 'bin' / executable
        require(compiler.is_file(), f'selected NDK compiler missing: {compiler}')
        wrapper = wrappers / name
        command = [str(compiler), '--target=aarch64-linux-android29', '--sysroot=' + str(tc / 'sysroot')]
        wrapper.write_text('#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n')
        wrapper.chmod(0o755)
        shutil.copy2(wrapper, E / ('android-' + name + '.sh'))
    require((tc / 'bin/ld.lld').is_file(), 'selected NDK linker missing')
    M['android_toolchain'] = {'mode': 'Chromium V8 compiler + complete NDK r26c consumer/sysroot', 'api': 29,
        'ndk_revision': '26.2.11394342', 'v8_clang_revision': REV,
        'wrappers': {p.name: sha(p) for p in wrappers.iterdir()}}
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("CARGO_FEATURE_", "BINDGEN_EXTRA_CLANG_ARGS", "RUSTY_V8_")) or key in ["DOCS_RS", "DENO_TRYBUILD", "DISABLE_CLANG", "GN_ARGS", "EXTRA_GN_ARGS", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "V8_FROM_SOURCE", "CC", "CXX", "AR", "CFLAGS", "CXXFLAGS"]:
            env.pop(key)
    env.update(GN=str(ROOT / "tools/gn/gn"), NINJA=str(ROOT / "tools/ninja/ninja"), PYTHON=sys.executable,
               CLANG_BASE_PATH=str(ROOT / "tools/clang"),
               RR_ANDROID_SYSROOT=str(tc / "sysroot"), RR_ANDROID_API="29", GN_ARGS="android_ndk_api_level=29",
               CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER=str(wrappers / "cc"),
               CC_aarch64_linux_android=str(wrappers / "cc"),
               CXX_aarch64_linux_android=str(wrappers / "cxx"), AR_aarch64_linux_android=str(tc / "bin/llvm-ar"),
               CARGO_TARGET_DIR=str(ROOT / "producer-target"), V8_FROM_SOURCE="1", PRINT_GN_ARGS="1")
    M["build_environment"] = {k: env[k] for k in env if k in ["GN", "NINJA", "PYTHON", "LIBCLANG_PATH", "CLANG_BASE_PATH", "GN_ARGS", "RR_ANDROID_SYSROOT", "RR_ANDROID_API"] or k.startswith(("CARGO_TARGET_", "CC_aarch64", "CXX_aarch64", "AR_aarch64"))}
    for tool in [env["GN"], env["NINJA"], str(ROOT / "tools/clang/bin/clang"), env["CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER"], sys.executable]:
        run([tool, "--version"], env=env)
    builtins = run([str(wrappers / 'cc'), '-print-libgcc-file-name'], env=env).strip()
    require(Path(builtins).is_file() and Path(builtins).resolve().is_relative_to(ROOT), 'Android compiler-rt builtins not resolved inside pinned toolchain')
    M['android_toolchain']['builtins'] = {'path': builtins, 'sha256': sha(builtins)}
    unwinds = sorted(tc.rglob('libunwind.a'))
    require(unwinds, 'selected NDK contains no libunwind archives')
    M['android_toolchain']['unwind_candidates'] = {str(p.relative_to(tc)): sha(p) for p in unwinds}
    M['android_toolchain']['consumer_tool_hashes'] = {str(p.relative_to(tc)): sha(p) for p in
        [tc / 'bin/clang', tc / 'bin/clang++', tc / 'bin/ld.lld', tc / 'bin/llvm-ar']}
    run([str(wrappers / 'cc'), '-print-search-dirs'], env=env)
    smoke = ROOT / 'android-link-smoke.c'
    smoke.write_text('int main(void) { return 0; }\n')
    run([str(wrappers / 'cc'), '-v', smoke, '-o', ROOT / 'android-link-smoke'], env=env)
    smoke_elf = run([str(ROOT / 'tools/clang/bin/llvm-readelf'), '-h', '-l', '-d', ROOT / 'android-link-smoke'])
    require('AArch64' in smoke_elf and '/system/bin/linker64' in smoke_elf and 'libc.so.6' not in smoke_elf, 'Android link smoke target mismatch')
    run([str(S / "third_party/rust-toolchain/bin/rustc"), "-Vv"])
    if (ndk / "source.properties").is_file():
        shutil.copy2(ndk / "source.properties", E / "ndk-source.properties")
    M["tool_hashes"] = {str(p.relative_to(ROOT)): sha(p) for p in [Path(env["GN"]), Path(env["NINJA"]), ROOT / "tools/clang/bin/clang", Path(env["CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER"])]}
    OBSERVER.stage("Toolchain prepared")
    env, M["libclang_preflight"] = libclang_preflight(ROOT, E, S, env, run, download, sha)
    M["build_environment"].update({k: env[k] for k in ["LIBCLANG_PATH", "LD_LIBRARY_PATH", "RR_LIBCLANG_RESOURCE"]})
    M["loader_scope"] = "Explicit file and companion directory, scoped to Cargo process trees; direct and exact bindgen smoke use same environment. No global shell exports."
    OBSERVER.stage("libclang preflight PASS")
    if MODE == "preflight":
        M["stage"] = "PREFLIGHT_PASS"
        M["result"] = "PREFLIGHT_PASS"
        save()
        return
    M["prepared_source_sha256"] = tracked(S)
    M["consumer_source_sha256"] = tracked(C)
    require(os.cpu_count() >= 4, "Expected four-vCPU runner")
    jobs = 4
    M["jobs"] = jobs
    M["stage"] = "A4"
    OBSERVER.stage("native build start")
    run(["rustup", "run", "1.91.0", "cargo", "build", "--locked", "--release", "--target", "aarch64-linux-android", "--features", "v8_enable_sandbox", "-j", str(jobs), "-vv"], S, env)
    require(tracked(S) == M["prepared_source_sha256"], "producer tracked source changed during compile")
    g = ROOT / "producer-target/aarch64-linux-android/release/gn_out"
    args = run([env["GN"], "args", g, "--list", "--json"], S, env)
    (E / "gn-args.json").write_text(args)
    options = {x["name"]: x.get("current", x["default"])["value"] for x in json.loads(args)}
    expected = {"target_os": '"android"', "target_cpu": '"arm64"', "v8_target_cpu": '"arm64"', "v8_enable_sandbox": "true", "v8_enable_pointer_compression": "true", "v8_enable_external_code_space": "true", "use_custom_libcxx": "true", "is_component_build": "false", "is_debug": "false"}
    require(all(options.get(k) == v for k, v in expected.items()), "GN security/target gate mismatch")
    M["features"] = expected
    OBSERVER.stage("artifact/hash staging")
    capture(ROOT, ROOT / "salvage-live", include_logs=False)
    for name in ["args.gn", "project.json"]:
        shutil.copy2(g / name, E / name)
    archive = g / "obj/librusty_v8.a"
    binding = g / "src_binding.rs"
    # LLVM bitcode may need separate review with the matching LLVM tools.
    headers = run([str(ROOT / "tools/clang/bin/llvm-readelf"), "-h", archive], allowed=(0, 1))
    machines = re.findall(r"Machine:\s+([^\n]+)", headers)
    require(machines and all("AArch64" in x for x in machines), "archive contains non-AArch64 ELF members")
    M["archive_member_review_required"] = True
    run([str(ROOT / "tools/clang/bin/llvm-ar"), "t", archive])
    run([str(ROOT / "tools/clang/bin/llvm-nm"), "--undefined-only", archive])
    gz = A / "librusty_v8_ptrcomp_sandbox_release_aarch64-linux-android.a.gz"
    with archive.open("rb") as inp, gz.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as out:
        shutil.copyfileobj(inp, out)
    b = A / "src_binding_ptrcomp_sandbox_release_aarch64-linux-android.rs"
    shutil.copy2(binding, b)
    M["outputs"] = {p.name: sha(p) for p in [archive, gz, b]}
    (A / "rusty_v8_ptrcomp_sandbox_release_aarch64-linux-android.sha256").write_text(f"{sha(gz)}  {gz.name}\n{sha(b)}  {b.name}\n")
    M["source_postbuild"] = {"producer": tracked(S), "consumer": tracked(C), "producer_lock": sha(S / "Cargo.lock"), "consumer_lock": sha(lock)}
    require(M["source_postbuild"]["producer"] == M["prepared_source_sha256"] and M["source_postbuild"]["producer_lock"] == M["sources"]["producer_lock"], "producer postbuild identity mismatch")
    M["stage"] = "A5"
    OBSERVER.consumer = True
    OBSERVER.stage("consumer-link start")
    (E / "consumer-result.json").write_text(json.dumps({"result": "STARTED", "pair": M["outputs"]}) + "\n")
    env.pop("V8_FROM_SOURCE")
    env.update(RUSTY_V8_ARCHIVE=str(gz), RUSTY_V8_SRC_BINDING_PATH=str(b), CARGO_TARGET_DIR=str(ROOT / "consumer-target"))
    M["consumer_pair"] = {"archive": str(gz), "archive_sha256": sha(gz), "binding": str(b), "binding_sha256": sha(b)}
    run(["rustup", "run", "1.95.0", "cargo", "build", "--locked", "--release", "--target", "aarch64-linux-android", "-p", "codex-code-mode-host", "--bin", "codex-code-mode-host", "-j", str(jobs), "-vv"], C / "codex-rs", env)
    require(tracked(C) == M["consumer_source_sha256"] and sha(lock) == M["sources"]["codex_lock"], "consumer source changed")
    require(sha(S / "Cargo.lock") == M["sources"]["producer_lock"], "producer lock changed")
    host = ROOT / "consumer-target/aarch64-linux-android/release/codex-code-mode-host"
    elf = run([str(ROOT / "tools/clang/bin/llvm-readelf"), "-h", "-l", "-d", "-n", host])
    require("AArch64" in elf and "/system/bin/linker64" in elf and "libc.so.6" not in elf, "consumer ELF target mismatch")
    run([str(ROOT / "tools/clang/bin/llvm-nm"), "--undefined-only", host])
    shutil.copy2(host, A / host.name)
    M["outputs"][host.name] = sha(host)
    M["source_postconsumer"] = {"producer": tracked(S), "consumer": tracked(C), "producer_lock": sha(S / "Cargo.lock"), "consumer_lock": sha(lock)}
    require(M["source_postconsumer"]["producer"] == M["prepared_source_sha256"], "producer identity changed during consumer")
    (E / "consumer-result.json").write_text(json.dumps({"result": "PASS", "host_sha256": sha(host)}) + "\n")
    OBSERVER.stage("consumer result", result="PASS", host_sha256=sha(host))
    M["stage"] = "A6_REVIEW_REQUIRED"
    M["result"] = "EVIDENCE_READY_FOR_REVIEW"
    M["note"] = "Build/link gates passed; Development must review native closure, inputs and all evidence before assigning PRODUCER_FEASIBLE. No PART B automation."


if __name__ == "__main__":
    for name in ["evidence", "logs", "downloads", "tools", "artifacts"]:
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    try:
        main()
    except Exception as exc:
        M["result"] = "RUN_BLOCKED_REVIEW_REQUIRED"
        M["failure"] = {"message": str(exc), "stage": M["stage"]}
        if OBSERVER.consumer:
            (E / "consumer-result.json").write_text(json.dumps({"result": "FAIL", "error": str(exc)}) + "\n")
            OBSERVER.stage("consumer result", result="FAIL", error=str(exc))
        print(str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
    finally:
        M["elapsed_seconds"] = round(time.time() - START, 1)
        M["ended_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save()
        capture(ROOT, ROOT / "salvage-live", include_logs=True)
