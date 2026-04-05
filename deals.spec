# -*- mode: python ; coding: utf-8 -*-
# Cross-platform onedir spec for PyInstaller.
# Set DEALS_ARTIFACT env var to control the output directory name
# (e.g. deals-linux-x64, deals-macos-arm64, deals-windows-x64).
# Defaults to 'deals' when unset.

import os

_artifact = os.environ.get('DEALS_ARTIFACT', 'deals')
_strip = os.name != 'nt'  # strip on Linux/macOS, skip on Windows

a = Analysis(
    ['src/audible_deals/__main__.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'IPython', 'pygments', 'jedi', 'black', 'pytest', 'astroid', 'lxml', 'scipy', 'pandas', 'setuptools', 'pkg_resources', 'Crypto', 'sqlite3', 'uvloop', 'blib2to3', 'multiprocessing'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='deals',
    debug=False,
    bootloader_ignore_signals=False,
    strip=_strip,
    upx=True,
    upx_exclude=[],
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=_strip,
    upx=True,
    upx_exclude=[],
    name=_artifact,
)
