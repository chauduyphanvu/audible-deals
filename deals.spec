# -*- mode: python ; coding: utf-8 -*-
# Cross-platform onedir spec for PyInstaller.
# Set DEALS_ARTIFACT env var to control the output directory name
# (e.g. deals-linux-x64, deals-macos-arm64, deals-windows-x64).
# Defaults to 'deals' when unset.

import importlib.util
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

_artifact = os.environ.get('DEALS_ARTIFACT', 'deals')
_strip = os.name != 'nt'  # strip on Linux/macOS, skip on Windows
_excludes = [
    'PIL',
    '_pytest',
    'anyio',
    'argparse',
    'asyncio',
    'bdb',
    'bz2',
    'cmd',
    'code',
    'codeop',
    'concurrent.futures.process',
    'doctest',
    'ftplib',
    'gzip',
    'http.server',
    'httpcore._backends.trio',
    'httpx._main',
    'lzma',
    'markdown_it',
    'mdurl',
    'pdb',
    'pydoc',
    'pydoc_data',
    'rich.__main__',
    'rich.markdown',
    'rich.syntax',
    'rich.traceback',
    'rsa.parallel',
    'socketserver',
    'tarfile',
    'unittest',
    'matplotlib',
    'numpy',
    'IPython',
    'pygments',
    'jedi',
    'black',
    'pytest',
    'astroid',
    'lxml',
    'scipy',
    'pandas',
    'setuptools',
    'pkg_resources',
    'Crypto',
    'uvloop',
    'blib2to3',
    'multiprocessing',
    # Optional keychain support (`pip install audible-deals[keyring]`) is not bundled:
    # it pulls in platform-specific backends that would grow every binary.
    'keyring',
]

# The full-screen results browser (`deals find -i`) is bundled when the `tui` extra
# (textual) is installed in the build environment; without it the prompt browser is
# the only one. Textual needs a few modules the list above would otherwise drop.
_tui = importlib.util.find_spec('textual') is not None
_hidden = collect_submodules('audible_deals.cli')
_datas = []
if _tui:
    _hidden += collect_submodules('audible_deals.tui') + collect_submodules('textual')
    _datas += collect_data_files('textual')
    _excludes = [
        name
        for name in _excludes
        if name
        not in {
            'markdown_it',
            'mdurl',
            'pygments',
            'rich.markdown',
            'rich.syntax',
            'rich.traceback',
            'asyncio',
            'argparse',
        }
    ]

a = Analysis(
    ['src/audible_deals/__main__.py'],
    pathex=[],
    binaries=[],
    datas=_datas,
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_excludes,
    noarchive=False,
    optimize=1,
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
