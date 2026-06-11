# -*- mode: python ; coding: utf-8 -*-
import sys, os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None
src_path = os.path.join(os.getcwd(), 'src')

a = Analysis(
    [os.path.join(src_path, 'main.py')],
    pathex=[src_path],
    binaries=collect_dynamic_libs('brainflow'),
    datas=[
        *collect_data_files('brainflow'),
        *collect_data_files('PyQt6'),
        (os.path.join(src_path, '软件图标.png'), '.'),
        ('LICENSE.txt', '.'),
    ],
    hiddenimports=[
        'brainflow',
        'brainflow.board_shim',
        'brainflow.data_filter',
        'scipy.signal',
        'scipy.fft',
        'numpy',
        'ctypes',
        'pynput',
        'pynput.mouse',
        'pynput.keyboard',
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
    name='HybridMI_BCI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    target_arch='arm64',
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='HybridMI_BCI',
)

app = BUNDLE(
    coll,
    name='HybridMI-BCI GUI.app',
    icon='AppIcon.icns',
    bundle_identifier='com.bci.hybridmi',
    info_plist={
        'CFBundleShortVersionString': '2.4.14',
        'CFBundleVersion': '2.4.14',
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
        'NSMicrophoneUsageDescription': 'BCI app requires serial port access for OpenBCI board',
        'NSBluetoothAlwaysUsageDescription': 'BCI app may use Bluetooth for OpenBCI board',
        'com.apple.security.automation.apple-events': True,
    },
)

# ── Post-build: create Icon\r at bundle root (classic macOS icon resource) ──
import shutil, subprocess
_bundle = os.path.join(os.getcwd(), 'dist', 'HybridMI-BCI GUI.app')
_icns_src = os.path.join(_bundle, 'Contents', 'Resources', 'AppIcon.icns')
_icon_dst = os.path.join(_bundle, 'Icon\r')  # "Icon" + carriage return
if os.path.exists(_icns_src):
    shutil.copy2(_icns_src, _icon_dst)
    subprocess.run(['/usr/bin/SetFile', '-a', 'V', _icon_dst], check=False)
    os.utime(_bundle, None)
    os.utime(os.path.join(_bundle, 'Contents', 'Info.plist'), None)
    print(f'[bci_mac.spec] 已创建 Icon\r 资源 → {_icon_dst}')
