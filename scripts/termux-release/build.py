#!/usr/bin/env python3
"""Exact-source cross-build using the preserved, hash-bound PART-A V8 pair."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1]
PINS = json.loads((HERE / "inputs.json").read_text())
ROOT = Path(os.environ["RR_ROOT"]).resolve()
ENV = dict(os.environ)
REPORT = {"status": "RUNNING", "commands": [], "stages": [], "v8_rebuilt": False}


def sha(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def save():
    p = ROOT / "evidence/result.json.tmp"
    p.write_text(json.dumps(REPORT, indent=2) + "\n")
    p.replace(ROOT / "evidence/result.json")


def stage(name):
    REPORT["stages"].append(
        {"stage": name, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    )
    save()
    print("STAGE", name, flush=True)


def run(argv, cwd=SOURCE, *, stdout_only=False):
    argv = list(map(str, argv))
    log = ROOT / "logs" / f"{len(REPORT['commands']):03d}.log"
    row = {"argv": argv, "cwd": str(cwd), "log": log.name, "exit": None}
    REPORT["commands"].append(row)
    save()
    print("COMMAND", json.dumps(row), flush=True)
    start = beat = time.monotonic()
    last = ""
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=ENV,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    sel = selectors.DefaultSelector()
    stdout_log = log.with_suffix(".stdout")
    stderr_log = log.with_suffix(".stderr")
    with log.open("xb") as f, stdout_log.open("xb") as out, stderr_log.open("xb") as err:
        sel.register(proc.stdout, selectors.EVENT_READ, out)
        sel.register(proc.stderr, selectors.EVENT_READ, err)
        while sel.get_map():
            for key, _ in sel.select(timeout=1):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    sel.unregister(key.fileobj)
                    continue
                key.data.write(data)
                key.data.flush()
                f.write(data)
                f.flush()
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
                lines = data.decode(errors="replace").strip().splitlines()
                if lines:
                    last = lines[-1][-500:]
            if time.monotonic() - beat >= 60:
                heart = {
                    "stage": REPORT["stages"][-1]["stage"],
                    "elapsed_s": round(time.monotonic() - start),
                    "last_line": last,
                    "load": os.getloadavg(),
                    "disk_free": shutil.disk_usage(ROOT).free,
                    "memory_kib": dict(
                        re.findall(
                            r"^(MemTotal|MemAvailable):\s+(\d+)",
                            Path("/proc/meminfo").read_text(),
                            re.M,
                        )
                    ),
                }
                (ROOT / "evidence/live.json").write_text(
                    json.dumps(heart, indent=2) + "\n"
                )
                print("HEARTBEAT", json.dumps(heart), flush=True)
                beat = time.monotonic()
    proc.stdout.close()
    proc.stderr.close()
    sel.close()
    row.update(
        exit=proc.wait(), elapsed_s=round(time.monotonic() - start, 2), sha256=sha(log)
    )
    row["streams"] = {
        "stdout": {"log": stdout_log.name, "sha256": sha(stdout_log)},
        "stderr": {"log": stderr_log.name, "sha256": sha(stderr_log)},
    }
    save()
    require(row["exit"] == 0, f"Command failed: {log.name}")
    return (stdout_log if stdout_only else log).read_text(errors="replace")


def download(pin):
    name = pin["url"].rsplit("/", 1)[-1]
    path = ROOT / "downloads" / name
    require(not path.exists(), "Download collision: " + name)
    run(["curl", "--fail", "--location", "--retry", "2", "--output", path, pin["url"]])
    require(sha(path) == pin["sha256"], "Download SHA mismatch: " + name)
    return path


def source_gate():
    require(
        run(["git", "rev-parse", "HEAD"]).strip() == os.environ["SOURCE_SHA"],
        "Source SHA mismatch",
    )
    require(
        not run(["git", "status", "--porcelain=v1", "--untracked-files=all"]).strip(),
        "Source tree changed",
    )
    require(
        sha(SOURCE / "codex-rs/Cargo.lock") == PINS["lock_sha256"],
        "Frozen lock changed",
    )
    run(["git", "merge-base", "--is-ancestor", PINS["baseline"], "HEAD"])


def main():
    ROOT.mkdir(mode=0o700)
    for name in [
        "evidence",
        "logs",
        "downloads",
        "artifacts",
        "pair",
        "host-tools",
        "target-packages",
    ]:
        (ROOT / name).mkdir()
    try:
        require(
            re.fullmatch("[0-9a-f]{40}", os.environ["SOURCE_SHA"]) is not None,
            "Full source SHA required",
        )
        REPORT["workflow"] = {
            k: os.getenv(k)
            for k in [
                "GITHUB_SHA",
                "GITHUB_REF",
                "GITHUB_WORKFLOW_REF",
                "GITHUB_WORKFLOW_SHA",
                "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT",
                "ImageOS",
                "ImageVersion",
            ]
        }
        REPORT["source_sha"] = os.environ["SOURCE_SHA"]
        REPORT["input_manifest_sha256"] = sha(HERE / "inputs.json")
        stage("source and preserved pair preflight")
        source_gate()
        require(
            sha(HERE / "producer-run.json") == PINS["producer_manifest_sha256"],
            "Producer manifest mismatch",
        )
        meta = json.loads(
            run(
                [
                    "gh",
                    "api",
                    "repos/cloudishBenne/codex/actions/artifacts/"
                    + str(PINS["pair_artifact_id"]),
                ]
            )
        )
        require(
            meta["name"] == PINS["pair_artifact"]
            and meta["digest"] == PINS["pair_artifact_digest"]
            and not meta["expired"],
            "Artifact identity/expiry mismatch",
        )
        require(
            meta["workflow_run"]["id"] == PINS["producer_run"]
            and meta["workflow_run"]["head_sha"] == PINS["producer_commit"],
            "Producer source/run mismatch",
        )
        REPORT["pair_artifact"] = meta
        run(
            [
                "gh",
                "run",
                "download",
                str(PINS["producer_run"]),
                "--repo",
                "cloudishBenne/codex",
                "--name",
                PINS["pair_artifact"],
                "--dir",
                ROOT / "pair",
            ]
        )
        ENV.pop("GH_TOKEN", None)
        ENV.pop("GITHUB_TOKEN", None)
        for name, digest in PINS["pair"].items():
            require(
                sha(ROOT / "pair" / name) == digest,
                "Preserved V8 pair mismatch: " + name,
            )
            shutil.copy2(ROOT / "pair" / name, ROOT / "artifacts" / name)
        archive = ROOT / "pair/librusty_v8.a"
        with (
            gzip.open(next((ROOT / "pair").glob("*.gz")), "rb") as inp,
            archive.open("xb") as out,
        ):
            shutil.copyfileobj(inp, out)
        require(
            sha(archive) == PINS["raw_archive_sha256"]
            and archive.stat().st_size == PINS["raw_archive_bytes"],
            "Raw archive mismatch",
        )
        stage("preserved archive and matching bindings verified")
        ndk_zip = download(PINS["ndk"])
        run(["unzip", "-q", ndk_zip, "-d", ROOT])
        tc = ROOT / "android-ndk-r26c/toolchains/llvm/prebuilt/linux-x86_64"
        require(
            "26.2.11394342"
            in (ROOT / "android-ndk-r26c/source.properties").read_text(),
            "NDK revision mismatch",
        )
        for pin in PINS["host_debs"]:
            run(["dpkg-deb", "-x", download(pin), ROOT / "host-tools"])
        run(["unzip", "-q", download(PINS["cmake"]), "-d", ROOT / "cmake"])
        for pin in PINS["target_packages"]:
            package = download(pin)
            require(
                package.stat().st_size == pin["bytes"], "Target package size mismatch"
            )
            run(["dpkg-deb", "-f", package, "Package", "Version", "Architecture"])
            run(["dpkg-deb", "-x", package, ROOT / "target-packages" / pin["name"]])
        wrappers = ROOT / "wrappers"
        wrappers.mkdir()
        import shlex

        for name, compiler in [("cc", "clang"), ("cxx", "clang++")]:
            wrapper = wrappers / name
            wrapper.write_text(
                "#!/bin/sh\nexec "
                + shlex.quote(str(tc / "bin" / compiler))
                + " --target=aarch64-linux-android29 --sysroot="
                + shlex.quote(str(tc / "sysroot"))
                + ' "$@"\n'
            )
            wrapper.chmod(0o755)
        hostbin = ROOT / "host-bin"
        hostbin.mkdir()
        (hostbin / "pkg-config").symlink_to(ROOT / "host-tools/usr/bin/pkgconf")
        for key in list(ENV):
            if key.startswith(
                ("RUSTY_V8_", "BINDGEN_EXTRA_CLANG_ARGS", "CARGO_FEATURE_")
            ) or key in [
                "V8_FROM_SOURCE",
                "RUSTFLAGS",
                "CARGO_ENCODED_RUSTFLAGS",
                "CC",
                "CXX",
                "AR",
                "CFLAGS",
                "CXXFLAGS",
                "PKG_CONFIG_ALLOW_CROSS",
                "OPENSSL_DIR",
                "OPENSSL_LIB_DIR",
                "OPENSSL_INCLUDE_DIR",
                "OPENSSL_STATIC",
            ]:
                ENV.pop(key)
        libdir = ROOT / "host-tools/usr/lib/x86_64-linux-gnu"
        libclang = libdir / "libclang-19.so.19"
        require(sha(libclang) == PINS["libclang_sha256"], "Host libclang mismatch")
        ssl = ROOT / "target-packages/openssl/data/data/com.termux/files/usr"
        ENV.update(
            CARGO_HOME=str(ROOT / "cargo-home"),
            RUSTUP_HOME=str(ROOT / "rustup-home"),
            CARGO_TARGET_DIR=str(ROOT / "target"),
            PATH=os.pathsep.join(
                [str(hostbin), str(ROOT / "cmake/cmake/data/bin"), ENV["PATH"]]
            ),
            LIBCLANG_PATH=str(libclang),
            LD_LIBRARY_PATH=str(libdir),
            RUSTY_V8_ARCHIVE=str(next((ROOT / "pair").glob("*.gz"))),
            RUSTY_V8_SRC_BINDING_PATH=str(next((ROOT / "pair").glob("*.rs"))),
            CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER=str(wrappers / "cc"),
            CC_aarch64_linux_android=str(wrappers / "cc"),
            CXX_aarch64_linux_android=str(wrappers / "cxx"),
            AR_aarch64_linux_android=str(tc / "bin/llvm-ar"),
            AARCH64_LINUX_ANDROID_OPENSSL_LIB_DIR=str(ssl / "lib"),
            AARCH64_LINUX_ANDROID_OPENSSL_INCLUDE_DIR=str(ssl / "include"),
            AARCH64_LINUX_ANDROID_OPENSSL_STATIC="0",
        )
        REPORT["environment"] = {
            k: v
            for k, v in ENV.items()
            if k.startswith(("RUSTY_V8_", "AARCH64_LINUX_ANDROID_OPENSSL_"))
            or k
            in [
                "CC_aarch64_linux_android",
                "CXX_aarch64_linux_android",
                "AR_aarch64_linux_android",
                "CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER",
                "LIBCLANG_PATH",
                "LD_LIBRARY_PATH",
                "PATH",
            ]
        }
        run(
            [
                "rustup",
                "toolchain",
                "install",
                "1.95.0",
                "--profile",
                "minimal",
                "--target",
                PINS["target"],
            ]
        )
        require(
            PINS["rust_commit"] in run(["rustup", "run", "1.95.0", "rustc", "-Vv"]),
            "Rust compiler identity mismatch",
        )
        for cmd in [
            ["cmake", "--version"],
            ["pkg-config", "--version"],
            ["cc", "--version"],
            [wrappers / "cc", "--version"],
        ]:
            run(cmd)
        require(
            "not found" not in run(["ldd", libclang]),
            "libclang loader dependencies missing",
        )
        run(
            [
                sys.executable,
                "-c",
                'import ctypes,sys; ctypes.CDLL(sys.argv[1]); print("LIBCLANG_LOAD_PASS")',
                libclang,
            ]
        )
        builtins = Path(run([wrappers / "cc", "--print-libgcc-file-name"]).strip())
        require(
            builtins.is_relative_to(tc) and sha(builtins) == PINS["builtins_sha256"],
            "NDK builtins mismatch",
        )
        members = run([tc / "bin/llvm-ar", "t", archive]).splitlines()
        require(len(members) == PINS["members"], "Archive member count mismatch")
        REPORT["archive_members"] = members
        smoke = ROOT / "openssl-smoke.c"
        smoke.write_text(
            "#include <openssl/ssl.h>\n#include <openssl/crypto.h>\nint main(void){SSL_CTX *c=SSL_CTX_new(TLS_method());unsigned long v=OpenSSL_version_num();SSL_CTX_free(c);return v==0;}\n"
        )
        run(
            [
                wrappers / "cc",
                smoke,
                "-I" + str(ssl / "include"),
                "-L" + str(ssl / "lib"),
                "-lssl",
                "-lcrypto",
                "-o",
                ROOT / "openssl-smoke",
            ]
        )
        elf = run(
            [
                tc / "bin/llvm-readelf",
                "-h",
                "-l",
                "-d",
                "--dyn-syms",
                ROOT / "openssl-smoke",
            ]
        )
        require(
            all(
                s in elf
                for s in [
                    "AArch64",
                    "/system/bin/linker64",
                    "[libssl.so.3]",
                    "[libcrypto.so.3]",
                    "SSL_CTX_new",
                    "OpenSSL_version_num",
                ]
            ),
            "Two-library Android C smoke failed",
        )
        stage("toolchain and OpenSSL two-library preflight PASS")
        cargo = ["rustup", "run", "1.95.0", "cargo"]
        metadata = json.loads(
            run(
                cargo
                + [
                    "metadata",
                    "--locked",
                    "--format-version",
                    "1",
                    "--filter-platform",
                    PINS["target"],
                ],
                SOURCE / "codex-rs",
                stdout_only=True,
            )
        )
        v8 = [p for p in metadata["packages"] if p["name"] == "v8"]
        require(
            len(v8) == 1 and v8[0]["version"] == "150.4.0", "Unexpected V8 dependency"
        )
        node = next(n for n in metadata["resolve"]["nodes"] if n["id"] == v8[0]["id"])
        require(
            set(node["features"])
            == {
                "default",
                "use_custom_libcxx",
                "v8_enable_pointer_compression",
                "v8_enable_sandbox",
            },
            "V8 feature identity mismatch",
        )
        REPORT["v8_features"] = node["features"]
        discovery = run(
            cargo
            + [
                "build",
                "--locked",
                "--release",
                "--target",
                PINS["target"],
                "-j",
                "4",
                "-vv",
                "-p",
                "openssl-sys",
            ],
            SOURCE / "codex-rs",
        )
        require(
            all(
                s in discovery
                for s in [
                    "cargo:rustc-link-lib=dylib=ssl",
                    "cargo:rustc-link-lib=dylib=crypto",
                    "cargo:rustc-link-search=native=" + str(ssl / "lib"),
                ]
            ),
            "OpenSSL target discovery evidence missing",
        )
        stage("openssl-sys target discovery PASS")
        REPORT["executables"] = {}
        for package, binary in [
            ("codex-cli", "codex"),
            ("codex-code-mode-host", "codex-code-mode-host"),
        ]:
            stage(binary + " compile/link start")
            run(
                cargo
                + [
                    "rustc",
                    "--locked",
                    "--release",
                    "--target",
                    PINS["target"],
                    "-j",
                    "4",
                    "-vv",
                    "-p",
                    package,
                    "--bin",
                    binary,
                    "--",
                    "-C",
                    "link-arg=" + str(builtins),
                    "-C",
                    "link-arg=-Wl,--trace-symbol=__clear_cache",
                    "-C",
                    "link-arg=-Wl,-Map=" + str(ROOT / "evidence" / f"{binary}.map"),
                ],
                SOURCE / "codex-rs",
            )
            src = ROOT / "target" / PINS["target"] / "release" / binary
            dst = ROOT / "artifacts" / binary
            require(src.is_file(), "Missing executable after successful command")
            shutil.copy2(src, dst)
            REPORT["executables"][binary] = {
                "sha256": sha(dst),
                "bytes": dst.stat().st_size,
            }
            save()
            stage(binary + " output copied and hashed")
            elf = run([tc / "bin/llvm-readelf", "-h", "-l", "-d", "-n", dst])
            (ROOT / "evidence" / f"{binary}.elf.txt").write_text(elf)
            require(
                "AArch64" in elf and "/system/bin/linker64" in elf,
                "Wrong executable target",
            )
            run([tc / "bin/llvm-nm", "--undefined-only", dst])
        source_gate()
        for name, digest in PINS["pair"].items():
            require(
                sha(ROOT / "pair" / name) == digest,
                "V8 pair changed during consumer build",
            )
        REPORT["status"] = "B5_RAW_EXECUTABLES_READY; B6_PACKAGE_AND_CLOSURE_PENDING"
        stage("both committed-source executables preserved; packaging review pending")
    except BaseException as exc:
        REPORT["status"] = "FAIL"
        REPORT["error"] = str(exc)
        raise
    finally:
        state = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=SOURCE,
            capture_output=True,
            text=True,
        )
        REPORT["source_postcondition"] = {
            "exit": state.returncode,
            "status": state.stdout,
            "error": state.stderr,
            "lock_sha256": sha(SOURCE / "codex-rs/Cargo.lock"),
        }
        save()
        shutil.copy2(HERE / "inputs.json", ROOT / "artifacts/inputs.json")
        shutil.copy2(
            ROOT / "evidence/result.json", ROOT / "artifacts/build-result.json"
        )


if __name__ == "__main__":
    main()
