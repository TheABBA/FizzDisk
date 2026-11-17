#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""One-shot release build for FizzDisk (installed app).

What it does:
  1. Reads the version from fizzdisk/version.py (bump it there before a release).
  2. Writes version_info.txt -- the Windows version resource (file/product
     version, company, description) that PyInstaller embeds into the exe.
  3. Runs PyInstaller ONEDIR -> dist/FizzDisk/ (a real installed-app layout,
     so it starts fast with no per-launch temp extraction).
  4. Packages that folder into a single Inno Setup installer
     -> dist/FizzDiskSetup-<version>.exe. Running it installs FizzDisk into its
     own directory (Program Files), creates Start Menu / Desktop shortcuts and
     an uninstaller, and shows one UAC prompt ("run as admin").
  5. Optionally code-signs the installer if a certificate is supplied.

Certificate for signing (optional). Provide one of:
  * set the FIZZDISK_CERT env var to a path of a .pfx/.p12 certificate, or
  * drop a signing_cert.pfx (and optional signing_cert.pfx password via
    FIZZDISK_CERT_PASS) into this folder.
If none is found the build still succeeds, it just isn't signed (an unsigned
installer may show a SmartScreen "first run" warning, but works fine).

Usage:  python build.py            # build the installer
        python build.py --sign     # force a signing attempt
"""

import os
import sys
import glob
import json
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


def find_iscc():
    st = shutil.which("iscc") or shutil.which("ISCC")
    if st:
        return st
    cands = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
        Path("C:/Program Files/Inno Setup 6/ISCC.exe"),
    ]
    for c in cands:
        if c.exists():
            return str(c)
    return None


def find_cert():
    cert = os.environ.get("FIZZDISK_CERT", "")
    if cert and os.path.exists(cert):
        return cert
    pfx = HERE / "signing_cert.pfx"
    return str(pfx) if pfx.exists() else None


def _azure_config():
    """Return (endpoint, account, profile) for Azure Trusted Signing, or None.

    Reads AZURE_SIGNING_ENDPOINT / _ACCOUNT / _PROFILE env vars, falling back to
    signing_config.json (endpoint/account/profile keys) so tokens never live in
    the repo. If any of the three is missing, returns None (no signing).
    """
    ep = os.environ.get("AZURE_SIGNING_ENDPOINT")
    ac = os.environ.get("AZURE_SIGNING_ACCOUNT")
    pf = os.environ.get("AZURE_SIGNING_PROFILE")
    cfg = HERE / "signing_config.json"
    if cfg.exists():
        try:
            d = json.loads(cfg.read_text(encoding="utf-8"))
            ep = ep or d.get("endpoint")
            ac = ac or d.get("account")
            pf = pf or d.get("profile")
        except Exception:
            pass
    if ep and ac and pf:
        return ep, ac, pf
    return None


def _dotnet_sign():
    """Locate the Microsoft 'sign' dotnet tool (preferred) in ~/.dotnet/tools."""
    for n in ("sign.exe", "sign"):
        here = os.path.join(os.path.expanduser("~"), ".dotnet", "tools", n)
        if os.path.isfile(here):
            return here
    for n in ("sign.exe", "sign"):
        p = shutil.which(n)
        if p:
            return p
    return None


def sign(exe, force=False):
    # Azure Trusted Signing (Microsoft 'sign' dotnet tool) - preferred.
    az = _azure_config()
    sgn = _dotnet_sign()
    if az and sgn:
        ep, ac, pf = az
        env = dict(os.environ)
        env["DOTNET_ROOT"] = os.path.join(os.path.expanduser("~"), ".dotnet")
        cmd = [sgn, "code", "artifact-signing",
               "--artifact-signing-endpoint", ep,
               "--artifact-signing-account", ac,
               "--artifact-signing-certificate-profile", pf,
               "--timestamp-url", "http://timestamp.acs.microsoft.com",
               "--description", "FizzDisk",
               "--description-url", "https://fizzdisk.philspad.com",
               str(exe)]
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        out = (r.stdout or r.stderr).strip()
        print("  [sign azure] " + (out or "(signed)"))
        if r.returncode != 0:
            print(f"  [sign azure] FAILED rc={r.returncode}\n{out}")
        return

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


def _bump_website_version(version: str):
    """Update the VERSION field in website/assets/config.js to match the build."""
    cfg = HERE / "website" / "assets" / "config.js"
    if not cfg.exists():
        return
    try:
        txt = cfg.read_text(encoding="utf-8")
        import re
        new = re.sub(r'VERSION:\s*"[^"]*"', f'VERSION: "{version}"', txt, count=1)
        if new != txt:
            cfg.write_text(new, encoding="utf-8")
            print(f"  [website] config.js VERSION -> {version}")
    except Exception:
        pass


def stage_bundled_ripper():
    """Populate bin/ with the bundled BD/UHD ripper so the installed app never
    needs an external ripper. The exe and its renamed core library
    (libfizzdisk64.dll) are staged/pre-patched under _dl/ ; here we only copy
    them into bin/ next to the app.
    """
    import shutil
    src = HERE / "_dl" / "ren"
    if not (src / "libfizzdisk64.dll").exists():
        print("  [rip] no _dl/ren bundled ripper found; skipping stage", file=sys.stderr)
        return
    exe_src = HERE / "_dl" / "ctl" / "fizzdisk-rip.exe"
    if exe_src.exists():
        (HERE / "bin" / "fizzdisk-rip.exe").write_bytes(exe_src.read_bytes())
        print("  [rip] staged bundled fizzdisk-rip.exe")
    else:
        print("  [rip] warning: no _dl/ctl/fizzdisk-rip.exe", file=sys.stderr)
    for name in ("libfizzdisk64.dll", "libffm64.dll", "libmqt64.dll",
                 "libmmbd.dll", "libmmbd64.dll", "libdriveio64.dll",
                 "blues.jar", "appdata.tar"):
        shutil.copy(str(src / name), str(HERE / "bin" / name))
    print("  [rip] staged bundled ripper files into bin/")


def main():
    version, app, desc = _read_version()
    print(f"Building {app} v{version} (onedir + Inno installer)...")
    write_version_resource(version, app, desc)
    stage_bundled_ripper()

    # Bump the version in the website's config.js so the footer and download
    # tracker stay in sync with the installer being built.
    _bump_website_version(version)

    # clean previous build output
    shutil.rmtree(HERE / "build", ignore_errors=True)
    shutil.rmtree(HERE / "dist", ignore_errors=True)

    # Write a version.txt in the dist download dir so the website's download
    # tracker can auto-detect which version is served (app.js fetches
    # download/version.txt instead of relying on a hardcoded config.js value).
    dist_dl = HERE / "dist" / "download"
    dist_dl.mkdir(parents=True, exist_ok=True)
    (dist_dl / "version.txt").write_text(version + "\n", encoding="utf-8")

    spec = HERE / "FizzDisk.spec"
    r = subprocess.run([sys.executable, "-m", "PyInstaller",
                        "--noconfirm", "--clean", str(spec)],
                       cwd=str(HERE))
    if r.returncode != 0:
        print("PyInstaller FAILED.")
        return r.returncode

    # sign the app executable (inside the onedir) before Inno packages it, so
    # the file installed to Program Files is signed too, not just the installer.
    app_exe = HERE / "dist" / "FizzDisk" / "FizzDisk.exe"
    if app_exe.exists() and _azure_config():
        print("  [sign] signing app exe (Azure Trusted Signing)...")
        sign(str(app_exe))

    iscc = find_iscc()
    if not iscc:
        print("ISCC (Inno Setup) not found \u2014 left the app in dist/FizzDisk/.")
        return 0

    iss = HERE / "installer" / "FizzDisk.iss"
    r = subprocess.run([iscc, f"/DVERSION={version}", str(iss)], cwd=str(HERE))
    if r.returncode != 0:
        print("Inno Setup FAILED.")
        return r.returncode

    setup = HERE / "dist" / f"FizzDiskSetup-{version}.exe"
    if setup.exists():
        sign(setup, force="--sign" in sys.argv)
        print(f"\nDONE \u2014 {setup}")
        print(f"  size: {setup.stat().st_size / (1024 * 1024):.1f} MB")
        print("  Installs FizzDisk into its own folder (Program Files) with")
        print("  Start Menu / Desktop shortcuts and an uninstaller (one UAC prompt).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
