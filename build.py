#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""One-shot release build for FizzDisk.

What it does:
  1. Reads the version from fizzdisk/version.py (bump it there before a release).
  2. Writes version_info.txt -- the Windows version resource (file/product
     version, company, description) that PyInstaller embeds into the exe.
  3. Runs PyInstaller ONEFILE (all binaries/data embedded -> a single .exe).
  4. Names the output dist/FizzDisk-<version>.exe.
  5. If signtool is available and a certificate is supplied, code-signs it.

Certificate for signing (optional). Provide one of:
  * set the FIZZDISK_CERT env var to a path of a .pfx/.p12 certificate, or
  * drop a signing_cert.pfx (and optional signing_cert.pfx password via
    FIZZDISK_CERT_PASS) into this folder.
If none is found the build still succeeds, it just isn't signed (an unsigned
single-file exe will trigger a SmartScreen "first run" warning, but work fine).

Usage:  python build.py            # release build
        python build.py --sign     # force signing attempt
"""

import os
import sys
import glob
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _read_version():
    from fizzdisk.version import __version__, APP_NAME, FILE_DESCRIPTION
    return __version__, APP_NAME, FILE_DESCRIPTION


def _norm_version(version):
    parts = [int(x or 0) for x in str(version).split(".")]
    parts = (parts + [0, 0, 0])[:4]
    return tuple(parts)


def write_version_resource(version, app, desc):
    v = _norm_version(version)
    txt = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v[0]}, {v[1]}, {v[2]}, {v[3]}),
    prodvers=({v[0]}, {v[1]}, {v[2]}, {v[3]}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', '{app}'),
        StringStruct('FileDescription', '{desc}'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', '{app}'),
        StringStruct('OriginalFilename', '{app}.exe'),
        StringStruct('ProductName', '{app}'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [0x0409, 1200])])
  ]
)
"""
    out = HERE / "version_info.txt"
    out.write_text(txt, encoding="utf-8")
    return out


def find_signtool():
    st = shutil.which("signtool")
    if st:
        return st
    for k in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(k)
        if not base:
            continue
        for pat in (f"{base}/Windows Kits/10/bin/*/x64/signtool.exe",
                    f"{base}/Windows Kits/10/bin/*/x64/signtool.exe"):
            for m in glob.glob(pat):
                if os.path.isfile(m):
                    return m
    return None


def find_cert():
    cert = os.environ.get("FIZZDISK_CERT", "")
    if cert and os.path.exists(cert):
        return cert
    pfx = HERE / "signing_cert.pfx"
    return str(pfx) if pfx.exists() else None


def sign(exe, force=False):
    st = find_signtool()
    if not st:
        print("  [sign] signtool not found \u2014 skipping signing.")
        return
    cert = find_cert()
    if not cert and not force:
        print("  [sign] no certificate (FIZZDISK_CERT / signing_cert.pfx) \u2014 skipped.")
        return
    cmd = [st, "sign", "/fd", "sha256", "/a",
           "/t", "http://timestamp.digicert.com"]
    if cert:
        cmd += ["/f", cert]
        if os.environ.get("FIZZDISK_CERT_PASS"):
            cmd += ["/p", os.environ["FIZZDISK_CERT_PASS"]]
    cmd.append(str(exe))
    r = subprocess.run(cmd, capture_output=True, text=True)
    print("  [sign] " + (r.stdout or r.stderr).strip() or "(signed)")


def main():
    version, app, desc = _read_version()
    print(f"Building {app} v{version} (onefile)...")
    write_version_resource(version, app, desc)

    # clean previous build output
    shutil.rmtree(HERE / "build", ignore_errors=True)
    shutil.rmtree(HERE / "dist", ignore_errors=True)

    spec = HERE / "FizzDisk.spec"
    r = subprocess.run([sys.executable, "-m", "PyInstaller",
                        "--noconfirm", "--clean", str(spec)],
                       cwd=str(HERE))
    if r.returncode != 0:
        print("PyInstaller FAILED.")
        return r.returncode

    src = HERE / "dist" / f"{app}.exe"
    dst = HERE / "dist" / f"{app}-v{version}.exe"
    if src.exists():
        src.replace(dst)

    force = "--sign" in sys.argv
    sign(dst, force=force)

    print(f"\nDONE \u2014 {dst}")
    print(f"  size: {dst.stat().st_size / (1024 * 1024):.1f} MB")
    print(f"  (single file, no _internal folder. Verify/SmartScreen: "
          f"signed={bool(find_cert() or force)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
