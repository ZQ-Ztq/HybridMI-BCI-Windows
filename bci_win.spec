# -*- mode: python ; coding: utf-8 -*-
"""
HybridMI-BCI GUI — Windows PyInstaller spec (单文件模式)
=======================================================
输出单个 .exe 文件，所有依赖打包在内。
"""

import sys, os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None
src_path = os.path.join(os.getcwd(), 'src')
ico_path = os.path.join(os.getcwd(), '软件图标.ico')

a = Analysis(
    [os.path.join(src_path, 'main.py')],
    pathex=[src_path],
    binaries=[],
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
    a.binaries,
    a.zipfiles,
    a.datas,
    name='HybridMI-BCI.exe',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ico_path if os.path.exists(ico_path) else None,
)
