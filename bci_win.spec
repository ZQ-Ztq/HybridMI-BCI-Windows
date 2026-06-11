# -*- mode: python ; coding: utf-8 -*-
"""
HybridMI-BCI GUI — Windows PyInstaller spec
============================================
Supports both 32-bit and 64-bit Windows builds.
Architecture is determined by the Python interpreter used to run PyInstaller:
  - 32-bit Python → 32-bit .exe
  - 64-bit Python → 64-bit .exe
"""
import sys, os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None
src_path = os.path.join(os.getcwd(), 'src')
ico_path = os.path.join(os.getcwd(), '软件图标.ico')

a = Analysis(
    [os.path.join(src_path, 'main.py')],
    pathex=[src_path],
    binaries=collect_dynamic_libs('brainflow'),
    datas=[
        *collect_data_files('brainflow'),
        *collect_data_files('PyQt6'),
        *([(os.path.join(src_path, '软件图标.png'), '.')] if os.path.exists(os.path.join(src_path, '软件图标.png')) else []),
    ],
    hiddenimports=[
        'brainflow',
        'brainflow.board_shim',
        'brainflow.data_filter',
        'scipy.signal',
        'scipy.fft',
        'numpy',
        'ctypes',
        'serial',
        'serial.tools',
        'serial.tools.list_ports',
        'PyQt6',
        'PyQt6.QtWidgets',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'signal_processor',
        'brainflow_streamer',
        'mouse_controller',
        # Windows-specific fallback
        'pynput',
        'pynput.mouse',
        'pynput._util',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='HybridMI-BCI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=ico_path if os.path.exists(ico_path) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='HybridMI-BCI',
)
