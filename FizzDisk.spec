# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all
import os

# The build script (build.py) writes version_info.txt with the Windows version
# resource (file/product version, company, description); if absent we build
# without one. UPX is OFF -- compressed bootloaders/payloads are a common
# antivirus false-positive source, and UPX isn't installed on this build box.
_VERSION_FILE = "version_info.txt" if os.path.exists("version_info.txt") else None

# bundle customtkinter (widgets) and pywinstyles (frosted-glass/Mica) fully
ctk_datas, ctk_bins, ctk_hidden = collect_all('customtkinter')
pws_datas, pws_bins, pws_hidden = collect_all('pywinstyles')

a = Analysis(
    ['FizzDisk.pyw'],
    pathex=[],
    binaries=[('bin/ffmpeg.exe', 'bin'), ('bin/ffprobe.exe', 'bin'), ('bin/libaacs.dll', 'bin'),
              ('bin/libbdplus.dll', 'bin'), ('bin/bluray.dll', 'bin'), ('bin/vcruntime140.dll', 'bin'),
              ('bin/vcruntime140_1.dll', 'bin'), ('bin/fizzdisk-rip.exe', 'bin'),
 ('bin/libmqt64.dll', 'bin'), ('bin/libffm64.dll', 'bin'), ('bin/libfizdisk64.dll', 'bin'),
 ('bin/libfizzbd64.dll', 'bin'), ('bin/libfizzbd.dll', 'bin'), ('bin/libdriveio64.dll', 'bin'),
 # audio-CD rip (libcdio-paranoia + runtime) -- error-corrected CD-DA reads
 ('bin/cd-paranoia.exe', 'bin'),
 ('bin/libcdio-21.dll', 'bin'), ('bin/libcdio_cdda-2.dll', 'bin'),
 ('bin/libcdio_paranoia-2.dll', 'bin'), ('bin/libiconv-2.dll', 'bin'),
 ('bin/libwinpthread-1.dll', 'bin')]
            + ctk_bins + pws_bins,
    datas=[('bin/keydb.cfg', 'bin'), ('bin/blues.jar', 'bin'), ('bin/appdata.tar', 'bin')]
           + ctk_datas + pws_datas,
    hiddenimports=['PIL', 'PIL.Image', 'PIL.ImageTk', 'PIL._tkinter_finder']
                  + ctk_hidden + pws_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Onefile: pass the binaries/data straight into the exe (exclude_binaries=False),
# so the user only gets a single FizzDisk.exe with no _internal folder.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FizzDisk',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=_VERSION_FILE,
)
# Onedir output: dist/FizzDisk/ ; an Inno Setup installer packages this folder
# (installed apps run instantly -- no per-launch temp extraction).
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='FizzDisk',
)
