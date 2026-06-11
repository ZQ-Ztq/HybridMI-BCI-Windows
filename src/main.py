"""
HybridMI-BCI GUI - Main Application  v2.4.14
============================================
PyQt6-based main window with:
  - Serial (USB) + Bluetooth connection panels
  - Auto device detection (dongle plug & play)
  - 8 head-top channels for hand+foot MI detection
  - Hand MI → LEFT/RIGHT, Foot MI → UP/DOWN
  - 4 auxiliary channels for MI confirmation
  - Diagonal movement (hand+foot combined)
  - Focus meter
  - Continuous 2D vector control for smooth cursor movement
  - 250 Hz sampling rate
  - Self-test mode (square drawing)
  - Start / Stop control

Third-party: BrainFlow SDK — Copyright (c) 2019 Andrey Parfenov (MIT)
             https://github.com/brainflow-dev/brainflow
"""

import sys
import os
import time
import threading
import logging
import platform
import math
from typing import Optional
import numpy as np
from scipy import signal as sp_signal

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QGroupBox, QGridLayout,
    QMessageBox, QFrame, QSizePolicy, QProgressBar, QDialog,
    QDialogButtonBox, QStatusBar, QSlider, QCheckBox, QSplitter,
    QTextEdit, QScrollArea, QTabWidget, QListWidget, QListWidgetItem,
    QStackedWidget, QSpacerItem
)
from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QObject, pyqtSlot, QSize, QPointF
)
from PyQt6.QtGui import (
    QColor, QPalette, QFont, QPainter, QBrush, QPen,
    QLinearGradient, QIcon, QPixmap
)

# Add src directory to path
sys.path.insert(0, os.path.dirname(__file__))

from signal_processor import SignalProcessor, SAMPLE_RATE, DISABLED_CHANNELS
from brainflow_streamer import BrainFlowStreamer
from mouse_controller import BCIMouseController

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

SYSTEM = platform.system()

# v2.4.2: Global space-to-exit listener (pynput) — v2.4.3: changed ESC → space
_SPACE_LISTENER_AVAILABLE = False
_KeyboardListener = None
_Key = None
_KeyCode = None
try:
    from pynput.keyboard import Listener as _KeyboardListener, Key as _Key, KeyCode
    _SPACE_LISTENER_AVAILABLE = True
    logger.info("Global space listener: pynput available")
except Exception as exc:
    logger.warning(f"Global space listener: pynput not available ({exc}) — "
                   f"double-space exit disabled, use GUI stop button instead")
APP_VERSION = "2.4.14"

# ── Resource path helper (works in both dev and PyInstaller bundles) ──
def _resource_path(relative_path: str) -> str:
    """Get absolute path to resource, works for dev and PyInstaller."""
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(__file__)
    return os.path.join(base, relative_path)


# ══════════════════════════════════════════════════════════════════════════
# Signals bridge (thread → GUI)
# ══════════════════════════════════════════════════════════════════════════

class BCISignals(QObject):
    channel_status_changed = pyqtSignal(dict)
    focus_changed          = pyqtSignal(float)
    board_status_changed   = pyqtSignal(str, str)
    all_channels_ready     = pyqtSignal()
    log_message            = pyqtSignal(str)
    bt_scan_finished       = pyqtSignal(list)
    test_status            = pyqtSignal(str, object)
    # v2.4.10: Thread-safe keyboard exit signal (emitted by pynput listener)
    keyboard_exit_requested = pyqtSignal()


# ══════════════════════════════════════════════════════════════════════════
# Channel LED widget
# ══════════════════════════════════════════════════════════════════════════

class ChannelLED(QFrame):
    COLORS = {
        "disabled": "#888888",
        "inactive": "#444444",
        "active":   "#00e676",
        "hand":     "#ff7043",
        "foot":     "#66bb6a",
        "aux_hand": "#ffb74d",
        "aux_foot": "#81c784",
        "blink":    "#ff9800",
        "focus":    "#42a5f5",
        "other":    "#78909c",
    }

    def __init__(self, ch_idx: int, name: str, role: str = "normal",
                 disabled: bool = False):
        super().__init__()
        self.ch_idx = ch_idx
        self.ch_name = name
        self.role = role
        self._state = "disabled" if disabled else "inactive"
        self.setFixedSize(QSize(80, 52))
        self.setToolTip(f"CH{ch_idx+1}: {name} [{role}]")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(1)

        self._dot = QLabel()
        self._dot.setFixedSize(12, 12)
        self._dot.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._label = QLabel(f"CH{ch_idx+1}")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setFont(QFont("Monospace", 7))

        role_short = {
            "hand": "★手", "foot": "★脚",
            "aux_hand": "辅手", "aux_foot": "辅脚",
            "blink": "眨眼", "focus": "专注",
            "other": "", "disabled": "关"
        }
        self._sublabel = QLabel(f"{name[:4]}{role_short.get(role, '')}")
        self._sublabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sublabel.setFont(QFont("Monospace", 6))

        layout.addWidget(self._dot, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._label)
        layout.addWidget(self._sublabel)

        self._update_style()

    def set_state(self, state: str):
        if state != self._state:
            self._state = state
            self._update_style()

    def _update_style(self):
        role_color_map = {
            "hand":     ("#ff7043", "#3d1a00"),
            "foot":     ("#66bb6a", "#0d2d1a"),
            "aux_hand": ("#ffb74d", "#2d1f00"),
            "aux_foot": ("#81c784", "#0d2118"),
            "blink":    ("#ff9800", "#663d00"),
            "focus":    ("#42a5f5", "#1a3a5c"),
            "other":    ("#78909c", "#1a2529"),
        }

        if self._state == "disabled":
            color = self.COLORS["disabled"]
            bg = "#1a1a2e"
        elif self._state == "inactive":
            color = self.COLORS["inactive"]
            bg = "#1a1a2e"
        else:
            rc = role_color_map.get(self.role, (self.COLORS["active"], "#16213e"))
            color = rc[0]
            bg = rc[1]

        self._dot.setStyleSheet(
            f"background: {color}; border-radius: 6px;"
        )
        self.setStyleSheet(
            f"QFrame {{ background: {bg}; border: 1px solid {color}; border-radius: 4px; }}"
        )
        text_color = "#aaaaaa" if self._state in ("disabled", "inactive") else "#ffffff"
        self._label.setStyleSheet(f"color: {text_color}; background: transparent;")
        self._sublabel.setStyleSheet(f"color: {text_color}; background: transparent;")


# ══════════════════════════════════════════════════════════════════════════
# Focus meter widget
# ══════════════════════════════════════════════════════════════════════════

class FocusMeter(QWidget):
    def __init__(self):
        super().__init__()
        self._score = 0.0
        self.setMinimumSize(200, 30)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(30)

    def set_score(self, score: float):
        self._score = max(0.0, min(1.0, score))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        fill = int(w * self._score)

        painter.setBrush(QBrush(QColor("#1a1a2e")))
        painter.setPen(QPen(QColor("#333355"), 1))
        painter.drawRoundedRect(0, 0, w, h, 4, 4)

        if fill > 0:
            grad = QLinearGradient(0, 0, w, 0)
            grad.setColorAt(0.0, QColor("#1565c0"))
            grad.setColorAt(0.5, QColor("#0288d1"))
            grad.setColorAt(1.0, QColor("#00e676"))
            painter.setBrush(QBrush(grad))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(2, 2, fill - 4, h - 4, 3, 3)

        painter.setPen(QPen(QColor("#ffffff")))
        painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        painter.drawText(0, 0, w, h, Qt.AlignmentFlag.AlignCenter,
                         f"专注度: {int(self._score * 100)}%")


# ══════════════════════════════════════════════════════════════════════════
# v2.2.2: Device status panel
# ══════════════════════════════════════════════════════════════════════════

class DeviceStatusWidget(QWidget):
    """Compact device status: connection, streaming, data rate, uptime."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(56)
        self.setMaximumHeight(64)

        layout = QGridLayout(self)
        layout.setSpacing(1)
        layout.setContentsMargins(6, 2, 6, 2)

        layout.addWidget(QLabel("连接:"), 0, 0)
        self._conn_label = QLabel("🔴 未连接")
        self._conn_label.setStyleSheet("color: #f85149; font-weight: bold; font-size: 10px;")
        layout.addWidget(self._conn_label, 0, 1)

        layout.addWidget(QLabel("数据:"), 0, 2)
        self._stream_label = QLabel("⬜ 空闲")
        self._stream_label.setStyleSheet("color: #8b949e; font-weight: bold; font-size: 10px;")
        layout.addWidget(self._stream_label, 0, 3)

        layout.addWidget(QLabel("速率:"), 1, 0)
        self._sps_label = QLabel("— SPS")
        self._sps_label.setStyleSheet("color: #8b949e; font-weight: bold; font-size: 10px;")
        layout.addWidget(self._sps_label, 1, 1)

        layout.addWidget(QLabel("样本:"), 1, 2)
        self._samples_label = QLabel("0")
        self._samples_label.setStyleSheet("color: #8b949e; font-size: 10px;")
        layout.addWidget(self._samples_label, 1, 3)

        for i in range(2):
            for j in [0, 2]:
                label = layout.itemAtPosition(i, j)
                if label and label.widget():
                    label.widget().setStyleSheet("color: #6e7681; font-size: 9px;")

    def set_connected(self, connected: bool):
        self._conn_label.setText("🟢 已连接" if connected else "🔴 未连接")
        self._conn_label.setStyleSheet(
            f"color: {'#56d364' if connected else '#f85149'}; font-weight: bold;")

    def set_streaming(self, streaming: bool):
        self._stream_label.setText("🟢 传输中" if streaming else "⬜ 空闲")
        self._stream_label.setStyleSheet(
            f"color: {'#56d364' if streaming else '#8b949e'}; font-weight: bold;")

    def set_sps(self, sps: float):
        if sps > 0:
            color = "#56d364" if sps > 50 else "#fb8500"
            self._sps_label.setText(f"{sps:.0f} SPS")
            self._sps_label.setStyleSheet(f"color: {color}; font-weight: bold;")
        else:
            self._sps_label.setText("— SPS")
            self._sps_label.setStyleSheet("color: #8b949e; font-weight: bold;")

    def set_total_samples(self, count: int):
        if count < 1000:
            self._samples_label.setText(str(count))
        elif count < 1000000:
            self._samples_label.setText(f"{count / 1000:.1f}k")
        else:
            self._samples_label.setText(f"{count / 1000000:.1f}M")


# ══════════════════════════════════════════════════════════════════════════
# Self-test square visualization widget
# ══════════════════════════════════════════════════════════════════════════

class SelfTestVisual(QWidget):
    """Visual feedback for the self-test cross-drawing progress."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(160, 160)
        self.setFixedSize(160, 160)
        self._progress = 0         # 0-3 (center→horiz→vert→return)
        self._active = False
        self._completed = False

    def set_progress(self, step: int):
        self._progress = step
        self._active = True
        self._completed = step >= 3
        self.update()

    def reset(self):
        self._progress = 0
        self._active = False
        self._completed = False
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        margin = 30
        cx, cy = w // 2, h // 2
        cross_w = w - 2 * margin
        cross_h = h - 2 * margin
        left, right = cx - cross_w // 2, cx + cross_w // 2
        top, bottom = cy - cross_h // 2, cy + cross_h // 2

        # Background
        painter.setBrush(QBrush(QColor("#0d1117")))
        painter.setPen(QPen(QColor("#30363d"), 1))
        painter.drawRoundedRect(0, 0, w, h, 6, 6)

        # Dim cross outline
        painter.setPen(QPen(QColor("#21262d"), 2, Qt.PenStyle.DashLine))
        painter.drawLine(left, cy, right, cy)    # horizontal guide
        painter.drawLine(cx, top, cx, bottom)    # vertical guide

        # Center dot
        if self._progress >= 0:
            painter.setPen(QPen(QColor("#58a6ff"), 1))
            painter.setBrush(QBrush(QColor("#58a6ff")))
            painter.drawEllipse(QPointF(cx, cy), 3, 3)

        # Horizontal bar (phase 1)
        if self._progress >= 1:
            painter.setPen(QPen(QColor("#00e676"), 3))
            painter.drawLine(left, cy, right, cy)

        # Vertical bar (phase 2)
        if self._progress >= 2:
            painter.setPen(QPen(QColor("#00e676"), 3))
            painter.drawLine(cx, top, cx, bottom)

        # Completed glow
        if self._completed:
            painter.setPen(QPen(QColor("#56d364"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(0, 0, w - 1, h - 1, 6, 6)

        # Center text
        painter.setPen(QPen(QColor("#8b949e")))
        painter.setFont(QFont("Monospace", 10))
        if not self._active:
            painter.drawText(0, 0, w, h, Qt.AlignmentFlag.AlignCenter,
                             "点击测试")
        elif self._completed:
            painter.setPen(QPen(QColor("#56d364")))
            painter.drawText(0, 0, w, h, Qt.AlignmentFlag.AlignCenter,
                             "✓ 测试通过")
        elif self._active:
            phase_labels = ["📍中心", "➖水平", "|垂直", "↩恢复"]
            label = phase_labels[min(self._progress, 3)]
            painter.drawText(0, 0, w, h, Qt.AlignmentFlag.AlignCenter, label)


# ══════════════════════════════════════════════════════════════════════════
# v2.2.1: Device detected dialog — auto-connect prompt
# ══════════════════════════════════════════════════════════════════════════

class DeviceDetectedDialog(QDialog):
    """Shown when an OpenBCI board is auto-detected via dongle."""

    def __init__(self, device_info: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设备检测")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setStyleSheet("""
            QDialog { background: #0d1117; }
            QLabel { color: #e6edf3; font-size: 14px; }
            QPushButton {
                padding: 10px 24px;
                border-radius: 8px;
                font-size: 14px;
                font-weight: bold;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(28, 28, 28, 28)

        # Icon + title
        icon_label = QLabel("🔌  设备接入")
        icon_label.setFont(QFont("Arial", 18, QFont.Weight.Bold))
        icon_label.setStyleSheet("color: #56d364;")

        # Device info
        name = device_info.get('name', '未知设备')
        port = device_info.get('port', '')
        desc = device_info.get('description', '')

        info_text = (
            f"<b>{name}</b><br><br>"
            f"端口：<code>{port}</code><br>"
            f"描述：{desc}<br><br>"
            f"<span style='color:#8b949e; font-size:12px;'>"
            f"📌 提示：请确保板子已开机，接收器已插入电脑 USB 口。"
            f"</span>"
        )
        info_label = QLabel(info_text)
        info_label.setWordWrap(True)
        info_label.setStyleSheet(
            "color: #c9d1d9; font-size: 13px; line-height: 1.7;"
        )

        # Buttons
        btn_box = QHBoxLayout()
        btn_box.setSpacing(12)

        self._btn_connect = QPushButton("✅  确认连接")
        self._btn_connect.setStyleSheet("""
            QPushButton {
                background: #238636; color: white;
                border: 1px solid #2ea043;
            }
            QPushButton:hover { background: #2ea043; }
        """)

        self._btn_ignore = QPushButton("⏭  暂不连接")
        self._btn_ignore.setStyleSheet("""
            QPushButton {
                background: #21262d; color: #8b949e;
                border: 1px solid #30363d;
            }
            QPushButton:hover { background: #30363d; color: #c9d1d9; }
        """)

        btn_box.addWidget(self._btn_ignore)
        btn_box.addWidget(self._btn_connect)

        layout.addWidget(icon_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(info_label)
        layout.addLayout(btn_box)

        self._btn_connect.clicked.connect(self.accept)
        self._btn_ignore.clicked.connect(self.reject)


# ══════════════════════════════════════════════════════════════════════════
# Confirmation dialog for cancelling control
# ══════════════════════════════════════════════════════════════════════════

class CancelControlDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("取消脑机接口控制")
        self.setModal(True)
        self.setMinimumWidth(380)
        self.setStyleSheet("""
            QDialog { background: #0d1117; }
            QLabel { color: #e6edf3; font-size: 14px; }
            QPushButton {
                padding: 8px 20px;
                border-radius: 6px;
                font-size: 13px;
                font-weight: bold;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        icon_label = QLabel("⚠️  确认操作")
        icon_label.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        icon_label.setStyleSheet("color: #f0a500;")

        msg = QLabel(
            "您即将取消脑机接口对鼠标的控制。\n\n"
            "取消后，鼠标将恢复正常手动控制模式。\n"
            "请确认您的意图。"
        )
        msg.setWordWrap(True)
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setStyleSheet("color: #c9d1d9; font-size: 13px; line-height: 1.6;")

        btn_box = QHBoxLayout()
        self._btn_confirm = QPushButton("✅  确认取消控制")
        self._btn_confirm.setStyleSheet("""
            QPushButton {
                background: #c62828; color: white;
                border: none;
            }
            QPushButton:hover { background: #d32f2f; }
        """)
        self._btn_cancel = QPushButton("↩  继续脑机控制")
        self._btn_cancel.setStyleSheet("""
            QPushButton {
                background: #1565c0; color: white;
                border: none;
            }
            QPushButton:hover { background: #1976d2; }
        """)

        btn_box.addWidget(self._btn_cancel)
        btn_box.addWidget(self._btn_confirm)

        layout.addWidget(icon_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(msg)
        layout.addLayout(btn_box)

        self._btn_confirm.clicked.connect(self.accept)
        self._btn_cancel.clicked.connect(self.reject)


# ══════════════════════════════════════════════════════════════════════════
# EEG Waveform widget  (v2.4.0)
# Scrolling real-time oscilloscope view for up to 8 EEG channels.
# Uses pure QPainter — no external dependencies.
# ══════════════════════════════════════════════════════════════════════════

class EEGWaveWidget(QWidget):
    """
    Scrolling EEG waveform display.

    - Maintains a ring-buffer of `buf_seconds` seconds per channel.
    - Channels are stacked vertically; each row auto-scales to its own
      peak-to-peak amplitude (soft smoothed auto-gain).
    - Channel colours match the ChannelLED role colours.
    - Push raw data in via push_data(data_2d) where data_2d is
      shape (n_channels_total, n_samples); only the channels listed
      in `ch_indices` are displayed.
    """

    # Role → colour map (matches ChannelLED.COLORS)
    ROLE_COLORS = {
        "hand":     "#ff7043",
        "foot":     "#66bb6a",
        "aux_hand": "#ffb74d",
        "aux_foot": "#81c784",
        "blink":    "#ff9800",
        "focus":    "#42a5f5",
        "other":    "#78909c",
        "disabled": "#555555",
    }
    DEFAULT_COLOR = "#58a6ff"

    # v2.4.12: Increased to 16 so all 8 active channels always visible
    MAX_DISPLAY_CH = 16

    def __init__(self, sample_rate: int = 250, buf_seconds: float = 5.0,
                 parent=None):
        super().__init__(parent)
        self.sample_rate = sample_rate
        self.buf_seconds = buf_seconds
        self._buf_len = int(sample_rate * buf_seconds)

        # Channel config — set via configure()
        self._ch_indices: list[int] = []    # indices into incoming 16-ch data
        self._ch_names:   list[str] = []
        self._ch_colors:  list[str] = []

        # Ring buffers:  list[np.ndarray shape (buf_len,)]
        self._buffers:    list = []
        self._write_pos:  int  = 0          # next write index (ring)
        self._filled:     int  = 0          # samples filled so far

        # ── v2.4.1: Diagnostics ──
        self._total_samples: int = 0        # total samples pushed (all time)
        self._push_count:    int = 0        # number of push_data calls
        self._last_push_ts:  float = 0.0    # timestamp of last push
        self._error_count:   int = 0        # number of push errors

        # Per-channel smoothed scale (µV half-range shown)
        self._scales:     list = []         # float per channel
        self._target_scales: list = []

        # Rendering
        self.setMinimumHeight(160)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setStyleSheet("background: #0d1117; border-radius: 4px;")

        # Timer — repaint at ~30 fps
        self._paint_timer = QTimer(self)
        self._paint_timer.setInterval(33)
        self._paint_timer.timeout.connect(self.update)
        self._paint_timer.start()

        # Thread safety
        self._lock = __import__('threading').Lock()

    # ── Public API ──────────────────────────────────────────────────────

    def configure(self, ch_defs: list):
        """
        Set which channels to display.

        ch_defs: list of (ch_index, name, role) tuples — same format as
                 the ch_defs list in _build_ui.  Only non-disabled channels
                 are shown, up to MAX_DISPLAY_CH.
        """
        from signal_processor import DISABLED_CHANNELS
        visible = [(idx, name, role) for idx, name, role in ch_defs
                   if idx not in DISABLED_CHANNELS
                   and role not in ("disabled",)]
        visible = visible[:self.MAX_DISPLAY_CH]

        with self._lock:
            self._ch_indices = [x[0] for x in visible]
            self._ch_names   = [x[1] for x in visible]
            self._ch_colors  = [self.ROLE_COLORS.get(x[2], self.DEFAULT_COLOR)
                                 for x in visible]
            n = len(visible)
            self._buffers       = [np.zeros(self._buf_len, dtype=np.float32)
                                    for _ in range(n)]
            self._scales        = [100.0] * n
            self._target_scales = [100.0] * n
            self._write_pos = 0
            self._filled    = 0

    def push_data(self, data: np.ndarray):
        """
        Accept raw data block from the BrainFlow streamer.

        data: np.ndarray shape (16, n_samples)
        """
        if not self._ch_indices:
            return

        # ── v2.4.1: Validate shape ──
        try:
            if not isinstance(data, np.ndarray) or data.ndim < 2:
                self._error_count += 1
                logger.warning(f"EEGWave push_data: bad shape {getattr(data, 'shape', 'N/A')}, "
                               f"type={type(data).__name__}")
                return
            n_samp = data.shape[1]
            if n_samp == 0:
                return
        except Exception as e:
            self._error_count += 1
            logger.warning(f"EEGWave push_data shape check error: {e}")
            return

        with self._lock:
            for row, ch_idx in enumerate(self._ch_indices):
                try:
                    if ch_idx >= data.shape[0]:
                        continue
                    chunk = data[ch_idx, :].astype(np.float32).ravel()
                    if len(chunk) != n_samp:
                        # Truncate or pad to expected length
                        if len(chunk) > n_samp:
                            chunk = chunk[:n_samp]
                        else:
                            padded = np.zeros(n_samp, dtype=np.float32)
                            padded[:len(chunk)] = chunk
                            chunk = padded
                    buf = self._buffers[row]
                    # Vectorised ring write
                    end = self._write_pos + n_samp
                    if end <= self._buf_len:
                        buf[self._write_pos:end] = chunk
                    else:
                        first = self._buf_len - self._write_pos
                        buf[self._write_pos:] = chunk[:first]
                        buf[:end - self._buf_len] = chunk[first:]
                    # Smooth auto-scale: target = peak-to-peak * 0.6, min 10 µV
                    pp = float(np.ptp(chunk))
                    if pp > 1.0:
                        self._target_scales[row] = max(pp * 0.6, 10.0)
                except Exception as e:
                    self._error_count += 1
                    logger.debug(f"EEGWave push_data ch {ch_idx} error: {e}")
                    continue

            # Advance write position after all channels
            self._write_pos = (self._write_pos + n_samp) % self._buf_len
            self._filled = min(self._filled + n_samp, self._buf_len)

            # Smooth scale towards target (EMA α=0.05)
            for i in range(len(self._scales)):
                self._scales[i] += (self._target_scales[i] - self._scales[i]) * 0.05

        # ── v2.4.1: Diagnostics (outside lock) ──
        self._total_samples += n_samp
        self._push_count += 1
        self._last_push_ts = time.time()


    # ── Painting ────────────────────────────────────────────────────────

    def paintEvent(self, event):
        from PyQt6.QtGui import QPainter, QPen, QColor, QFont
        from PyQt6.QtCore import Qt

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        w = self.width()
        h = self.height()

        # Background
        painter.fillRect(0, 0, w, h, QColor("#0d1117"))

        with self._lock:
            n_ch = len(self._ch_indices)
            total = self._total_samples
            pushes = self._push_count

            if n_ch == 0:
                # No channels configured
                painter.setPen(QColor("#f85149"))
                painter.setFont(QFont("Arial", 11))
                painter.drawText(0, 0, w, h,
                    Qt.AlignmentFlag.AlignCenter,
                    "未配置显示通道 — 请检查通道设置")
                painter.end()
                return

            if self._filled < 2:
                # No data yet — show diagnostic info
                painter.setPen(QColor("#3d444d"))
                painter.setFont(QFont("Arial", 12))
                line1 = "等待数据…"
                painter.drawText(0, int(h * 0.35), w, 30,
                    Qt.AlignmentFlag.AlignCenter, line1)

                painter.setPen(QColor("#555555"))
                painter.setFont(QFont("Monospace", 9))
                line2 = (f"已配置 {n_ch} 个通道 | "
                         f"收到 {total} 样本 ({pushes} 次推送)")
                painter.drawText(0, int(h * 0.35) + 28, w, 22,
                    Qt.AlignmentFlag.AlignCenter, line2)

                if pushes == 0:
                    line3 = "提示：数据流未到达，请确认已点击「连接」并启动数据流"
                    painter.drawText(0, int(h * 0.35) + 52, w, 22,
                        Qt.AlignmentFlag.AlignCenter, line3)

                painter.end()
                return

            row_h = h / n_ch
            buf_len = self._buf_len
            filled = self._filled
            wp = self._write_pos

            # Honour _display_seconds if set (from time selector widget)
            display_secs = getattr(self, '_display_seconds', 5)
            max_draw = int(display_secs * self.sample_rate)
            # Number of samples to draw = min(filled, display window, buf)
            n_draw = min(filled, max_draw, buf_len)
            if n_draw < 2:
                painter.setPen(QColor("#3d444d"))
                painter.drawText(0, 0, w, h,
                    Qt.AlignmentFlag.AlignCenter, "积累数据中…")
                painter.end()
                return
            x_scale = (w - 82) / max(n_draw - 1, 1)   # 82px reserved for label

            # Grid lines (time markers every ~1 second)
            LABEL_W = 82   # pixels reserved for channel label on left
            painter.setPen(QPen(QColor("#1e2a38"), 1))
            secs = int(n_draw / self.sample_rate)
            for s in range(1, secs + 1):
                x = int(LABEL_W + (n_draw - s * self.sample_rate) * x_scale)
                if LABEL_W < x < w:
                    painter.drawLine(x, 0, x, h)
                # Time label at bottom
                painter.setPen(QColor("#3d444d"))
                painter.drawText(x - 10, h - 2, f"-{s}s")
                painter.setPen(QPen(QColor("#1e2a38"), 1))

            font = QFont("Monospace", 9)
            painter.setFont(font)

            for row in range(n_ch):
                buf = self._buffers[row]
                scale = max(self._scales[row], 1.0)
                color = QColor(self._ch_colors[row])
                center_y = row_h * (row + 0.5)

                # Row separator
                painter.setPen(QPen(QColor("#21262d"), 1))
                if row > 0:
                    painter.drawLine(0, int(row_h * row),
                                     w, int(row_h * row))

                # Channel label (left panel)
                painter.fillRect(0, int(row_h * row) + 1, LABEL_W - 2,
                                 int(row_h) - 1, QColor("#0d1117"))
                painter.setPen(QColor(self._ch_colors[row]))
                painter.drawText(4, int(center_y - row_h * 0.5) + 2,
                                 LABEL_W - 6, int(row_h),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 self._ch_names[row])

                # µV scale label
                painter.setPen(QColor("#3d444d"))
                painter.drawText(4, int(row_h * (row + 1)) - 2,
                                 LABEL_W - 6, 14,
                                 Qt.AlignmentFlag.AlignLeft,
                                 f"±{scale:.0f}µV")

                # Vertical divider after label area
                painter.setPen(QPen(QColor("#21262d"), 1))
                painter.drawLine(LABEL_W, int(row_h * row),
                                 LABEL_W, int(row_h * (row + 1)))

                # Centre line
                painter.setPen(QPen(QColor("#1e2a38"), 1))
                painter.drawLine(LABEL_W, int(center_y), w, int(center_y))

                # Waveform — read n_draw samples ending at write_pos
                pen = QPen(color, 1)
                painter.setPen(pen)

                # Build ordered sample array from ring buffer
                start_idx = (wp - n_draw) % buf_len
                if start_idx + n_draw <= buf_len:
                    samples = buf[start_idx:start_idx + n_draw]
                else:
                    first = buf_len - start_idx
                    samples = np.concatenate([buf[start_idx:], buf[:n_draw - first]])

                half_row = row_h * 0.44
                prev_x = prev_y = None
                step = max(1, n_draw // (w - LABEL_W))  # 1 sample per pixel
                for si in range(0, n_draw, step):
                    px = int(LABEL_W + si * x_scale)
                    py = int(center_y - (samples[si] / scale) * half_row)
                    py = max(int(row_h * row) + 1,
                             min(py, int(row_h * (row + 1)) - 1))
                    if prev_x is not None:
                        painter.drawLine(prev_x, prev_y, px, py)
                    prev_x, prev_y = px, py

        painter.end()




class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"HybridMI-BCI GUI  v{APP_VERSION}")
        self.setMinimumSize(960, 740)
        self.resize(1060, 780)

        # Core components
        self.signals = BCISignals()
        self.processor = SignalProcessor(SAMPLE_RATE)
        self.streamer = BrainFlowStreamer()
        self.mouse_ctrl = BCIMouseController()

        # State
        self._connected = False
        self._streaming = False
        self._bci_active = False
        self._bt_devices = []
        self._test_running = False
        self._detected_device = None   # v2.2.1: last detected device info
        self._device_dialog_shown = False  # prevent duplicate dialogs
        self._last_error_time: float = 0.0  # v2.3.3: throttle repeated error signals

        # v2.4.3: Global double-space to exit BCI control (was ESC in v2.4.2)
        self._space_listener = None           # pynput keyboard listener
        self._last_space_time: float = 0.0    # timestamp of last space press

        # v2.4.6: Raw EEG → mouse (CH5/13 → dx, CH6/14 → dy)
        # No bandpass filtering — only 50+60 Hz notch for power-line noise.
        # Uses scipy SOS notch filters (pre-designed at init).
        self._notch_sos_50: np.ndarray | None = None  # 50 Hz notch SOS
        self._notch_sos_60: np.ndarray | None = None  # 60 Hz notch SOS
        self._raw_control_call_count: int = 0          # debug: calls since last log

        # v2.4.8: Adaptive DC baseline tracking per channel (EMA)
        self._dc_baseline_ch5: float | None  = None
        self._dc_baseline_ch6: float | None  = None
        self._dc_baseline_ch13: float | None = None
        self._dc_baseline_ch14: float | None = None

        # v2.4.11: Previous filtered means for delta (change) detection
        self._prev_ch5: float | None  = None
        self._prev_ch6: float | None  = None
        self._prev_ch13: float | None = None
        self._prev_ch14: float | None = None

        # v2.4.13: Simulation mode — random 8-direction movement state
        self._sim_dir: tuple | None = None      # (dx, dy) current sim direction
        self._sim_steps: int = 0                 # steps remaining in this direction

        # v2.4.14: Simulation waveform phase accumulator
        self._sim_wave_phase: float = 0.0

        # v2.2.2: Data rate tracking
        self._data_count = 0            # total samples received
        self._data_t0 = 0.0             # first sample timestamp
        self._data_last_t = 0.0         # last sample timestamp
        self._sps_counter = 0           # samples counted in current SPS window
        self._sps_window_start = 0.0    # start of current 1-sec SPS window
        self._current_sps = 0.0         # current samples/sec

        # v2.4.5: Wall-clock fallback for SPS (timestamps may be unreliable)
        self._sps_wall_counter: int = 0
        self._sps_wall_start: float = 0.0

        # v2.4.7: heartbeat counter (incremented BEFORE any processing)
        self._data_heartbeat: int = 0     # raw call count, updated immediately
        self._last_on_data_err: str = ""  # last error logged for dedup

        # Wire up processor callbacks (v2.4.12: removed blink/mi event callbacks)
        self.processor.on_vector = lambda dx, dy, lbl: self._on_vector(dx, dy, lbl)
        self.processor.on_focus  = lambda s:  self.signals.focus_changed.emit(s)
        self.processor.on_ready  = lambda:    self.signals.all_channels_ready.emit()
        self.streamer.data_callback   = self._on_data
        self.streamer.status_callback = lambda s, d: self.signals.board_status_changed.emit(s, d)

        # Connect signals (v2.4.12: removed blink_event/mi_event)
        self.signals.channel_status_changed.connect(self._update_channel_leds)
        self.signals.focus_changed.connect(self._update_focus)
        self.signals.board_status_changed.connect(self._on_board_status)
        self.signals.all_channels_ready.connect(self._on_all_channels_ready)
        self.signals.log_message.connect(self._append_log)
        self.signals.bt_scan_finished.connect(self._on_bt_scan_finished)
        self.signals.test_status.connect(self._on_test_status)
        # v2.4.10: Keyboard exit signal — emitted by pynput listener thread
        self.signals.keyboard_exit_requested.connect(self._stop_bci)

        # Mouse controller status
        self.mouse_ctrl.on_status_change = lambda s: self.signals.log_message.emit(
            f"🖱️ 鼠标控制: {s}"
        )

        self._build_ui()

        # Channel status refresh timer
        self._ch_timer = QTimer()
        self._ch_timer.setInterval(500)
        self._ch_timer.timeout.connect(self._refresh_channel_status)
        self._ch_timer.start()

        # v2.4.11: Device auto-detection timer (every 5 seconds)
        self._device_timer = QTimer()
        self._device_timer.setInterval(5000)
        self._device_timer.timeout.connect(self._check_for_device)
        self._device_timer.start()

        # v2.2.2: Device status refresh timer (every 1 second)
        self._devstat_timer = QTimer()
        self._devstat_timer.setInterval(1000)
        self._devstat_timer.timeout.connect(self._refresh_device_status)
        self._devstat_timer.start()

        # Apply dark stylesheet
        self._apply_stylesheet()

        # v2.4.6: Pre-design 50+60 Hz notch filters (SOS form for stability)
        self._init_notch_filters()

        # Populate serial ports
        self._refresh_ports()

    # ── UI Construction ────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Menu bar ──
        menubar = self.menuBar()
        menubar.setStyleSheet("QMenuBar { background: #0d1117; color: #c9d1d9; font-size: 13px; }"
                              "QMenuBar::item:selected { background: #1f6feb; }"
                              "QMenu { background: #161b22; color: #c9d1d9; border: 1px solid #30363d; }"
                              "QMenu::item:selected { background: #1f6feb; }")
        help_menu = menubar.addMenu("帮助")
        about_action = help_menu.addAction("关于 HybridMI-BCI GUI")
        about_action.triggered.connect(self._show_about)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(12, 12, 12, 12)

        # Title bar
        title_bar = self._make_title_bar()
        main_layout.addWidget(title_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left panel: connection tabs + channels + controls
        left_panel = self._make_left_panel()
        splitter.addWidget(left_panel)

        # Right panel: status + scope + test + log + status
        right_panel = self._make_right_panel()
        splitter.addWidget(right_panel)

        splitter.setSizes([640, 400])
        main_layout.addWidget(splitter, 1)

        # Status bar
        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._statusbar.showMessage("就绪 — 请接入 OpenBCI 接收器并开启板子，或手动选择串口连接")

    def _show_about(self):
        """Show About dialog with BrainFlow attribution."""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QTextEdit, QPushButton
        dlg = QDialog(self)
        dlg.setWindowTitle("关于 HybridMI-BCI GUI")
        dlg.setMinimumSize(520, 440)
        dlg.setStyleSheet("""
            QDialog { background: #0d1117; color: #c9d1d9; }
            QLabel { color: #c9d1d9; }
            QTextEdit { background: #161b22; color: #c9d1d9; border: 1px solid #30363d;
                        border-radius: 6px; padding: 12px; font-size: 12px; }
            QPushButton { background: #1f6feb; color: white; border: none;
                          border-radius: 6px; padding: 8px 24px; font-size: 13px; }
            QPushButton:hover { background: #388bfd; }
        """)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        title = QLabel(f"<h2>HybridMI-BCI GUI</h2><p>版本 {APP_VERSION}</p>")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        info = QTextEdit()
        info.setReadOnly(True)
        info.setHtml(f"""
<p><b>HybridMI-BCI GUI</b> 是一款基于脑机接口（BCI）的鼠标控制系统，
通过 OpenBCI Cyton+Daisy 硬件采集脑电信号，利用运动想象（MI）实现光标控制。</p>

<h3>第三方开源组件</h3>

<p><b>BrainFlow SDK</b><br>
Copyright &copy; 2019 Andrey Parfenov<br>
Licensed under the <a href="https://github.com/brainflow-dev/brainflow/blob/master/LICENSE"
style="color:#58a6ff;">MIT License</a><br>
<a href="https://github.com/brainflow-dev/brainflow"
style="color:#58a6ff;">https://github.com/brainflow-dev/brainflow</a></p>

<p style="color:#8b949e; font-size:11px;">
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:<br><br>
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.</p>

<p style="color:#8b949e; font-size:11px;">
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.</p>

<h3>本软件许可</h3>
<p style="color:#8b949e; font-size:11px;">Copyright &copy; 2026. 保留所有权利。</p>
""")
        layout.addWidget(info)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        dlg.exec()

    def _make_title_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("titleBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 4, 8, 4)

        logo = QLabel()
        logo_pixmap = QPixmap(_resource_path("软件图标.png"))
        logo_pixmap = logo_pixmap.scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation)
        logo.setPixmap(logo_pixmap)
        logo.setFixedSize(56, 56)
        logo.setStyleSheet("background: transparent; border: none;")

        title = QLabel("HybridMI-BCI GUI")
        title.setFont(QFont("Arial", 17, QFont.Weight.Bold))
        title.setStyleSheet("color: #58a6ff;")

        version_badge = QLabel(f" v{APP_VERSION}")
        version_badge.setStyleSheet(
            "color: #ffab00; font-size: 13px; font-weight: bold;"
        )

        subtitle = QLabel(f"OpenBCI Cyton+Daisy  •  8头顶通道 手+脚MI  •  {platform.system()}")
        subtitle.setFont(QFont("Arial", 10))
        subtitle.setStyleSheet("color: #8b949e;")

        layout.addWidget(logo)
        layout.addSpacing(8)
        layout.addWidget(title)
        layout.addWidget(version_badge)
        layout.addSpacing(16)
        layout.addWidget(subtitle)
        layout.addStretch()

        self._system_badge = QLabel(f"  {platform.system()} {platform.machine()}  ")
        self._system_badge.setStyleSheet(
            "background: #1f6feb; color: white; border-radius: 8px;"
            "padding: 2px 8px; font-size: 11px;"
        )
        layout.addWidget(self._system_badge)
        return bar

    def _make_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(10)
        layout.setContentsMargins(0, 0, 0, 0)

        # ── Connection tabs: Serial | Bluetooth ──
        conn_group = QGroupBox("板子连接")
        conn_layout = QVBoxLayout(conn_group)

        conn_tabs = QTabWidget()
        conn_tabs.setStyleSheet("""
            QTabWidget::pane { border: 1px solid #30363d; border-radius: 4px; }
            QTabBar::tab { padding: 6px 16px; background: #161b22;
                           border: 1px solid #30363d; color: #8b949e; }
            QTabBar::tab:selected { background: #0d2d4a; color: #58a6ff;
                                    border-bottom: 2px solid #58a6ff; }
        """)

        # Tab 1: Serial (USB)
        serial_tab = QWidget()
        serial_layout = QGridLayout(serial_tab)

        serial_layout.addWidget(QLabel("串口:"), 0, 0)
        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(140)
        serial_layout.addWidget(self._port_combo, 0, 1)

        self._refresh_btn = QPushButton("🔄 刷新")
        self._refresh_btn.setFixedWidth(70)
        self._refresh_btn.clicked.connect(self._refresh_ports)
        serial_layout.addWidget(self._refresh_btn, 0, 2)

        self._connect_btn = QPushButton("🔌 连接")
        self._connect_btn.setObjectName("connectBtn")
        self._connect_btn.clicked.connect(self._toggle_connect)
        serial_layout.addWidget(self._connect_btn, 0, 3)

        self._sim_check = QCheckBox("模拟模式 (无需硬件)")
        self._sim_check.setChecked(True)
        serial_layout.addWidget(self._sim_check, 1, 0, 1, 4)

        conn_tabs.addTab(serial_tab, "🔌 串口 (USB)")

        # Tab 2: Bluetooth
        bt_tab = QWidget()
        bt_layout = QVBoxLayout(bt_tab)
        bt_layout.setSpacing(6)

        bt_top = QHBoxLayout()
        self._bt_scan_btn = QPushButton("🔍 搜索蓝牙设备")
        self._bt_scan_btn.setObjectName("btScanBtn")
        self._bt_scan_btn.clicked.connect(self._scan_bluetooth)
        self._bt_scan_btn.setMinimumHeight(32)
        bt_top.addWidget(self._bt_scan_btn)

        self._bt_status_label = QLabel("就绪 — 点击搜索发现设备")
        self._bt_status_label.setStyleSheet("color: #8b949e; font-size: 11px;")
        bt_top.addWidget(self._bt_status_label, 1)
        bt_layout.addLayout(bt_top)

        self._bt_device_list = QListWidget()
        self._bt_device_list.setMaximumHeight(100)
        self._bt_device_list.setStyleSheet(
            "QListWidget { background: #0d1117; border: 1px solid #30363d; "
            "border-radius: 4px; color: #c9d1d9; font-size: 12px; }"
            "QListWidget::item { padding: 4px 8px; }"
            "QListWidget::item:selected { background: #0d2d4a; color: #58a6ff; }"
        )
        bt_layout.addWidget(self._bt_device_list)

        self._bt_connect_btn = QPushButton("📡 连接蓝牙设备")
        self._bt_connect_btn.setObjectName("btConnectBtn")
        self._bt_connect_btn.setMinimumHeight(32)
        self._bt_connect_btn.setEnabled(False)
        self._bt_connect_btn.clicked.connect(self._connect_bluetooth)
        bt_layout.addWidget(self._bt_connect_btn)

        conn_tabs.addTab(bt_tab, "📡 蓝牙")

        conn_layout.addWidget(conn_tabs)
        layout.addWidget(conn_group)

        # ── v2.2 8头顶通道 手+脚 MI 监视器 ──
        ch_group = QGroupBox("8头顶通道状态 — 手部+脚部 运动想象 (v2.2)")
        ch_grid = QGridLayout(ch_group)
        ch_grid.setSpacing(4)

        self._ch_leds = {}
        # v2.2: 8 head-top channels for hand+foot MI:
        #   Left  hemi: CH4(Aux手) CH5(手L★) CH6(脚L★) CH7(Aux脚)
        #   Right hemi: CH12(Aux手) CH13(手R★) CH14(脚R★) CH15(Aux脚)
        #   Other: CH1(眨眼) CH2(眨眼) CH3(专注) CH8(禁用)
        #          CH9(其他) CH10(其他) CH11(专注) CH16(禁用)
        ch_defs = [
            # Row 0: Left hemisphere (head-top)
            (0,  "CH1(眨眼)",   "blink"),    (1,  "CH2(眨眼)",  "blink"),
            (2,  "CH3(专注)",   "focus"),    (3,  "CH4(Aux手L)", "aux_hand"),
            (4,  "CH5(★手L)",   "hand"),     (5,  "CH6(★脚L)",  "foot"),
            (6,  "CH7(Aux脚L)", "aux_foot"), (7,  "CH8(禁用)",   "disabled"),
            # Row 1: Right hemisphere + other
            (8,  "CH9(眨眼)",   "blink"),    (9,  "CH10(眨眼)", "blink"),
            (10, "CH11(专注)",  "focus"),    (11, "CH12(Aux手R)","aux_hand"),
            (12, "CH13(★手R)",  "hand"),     (13, "CH14(★脚R)", "foot"),
            (14, "CH15(Aux脚R)","aux_foot"), (15, "CH16(禁用)",  "disabled"),
        ]
        for i, (idx, name, role) in enumerate(ch_defs):
            disabled = idx in DISABLED_CHANNELS
            led = ChannelLED(idx, name, role=role, disabled=disabled)
            self._ch_leds[idx] = led
            ch_grid.addWidget(led, i // 8, i % 8)

        layout.addWidget(ch_group)

        # ── v2.4.0: EEG Waveform display ──
        eeg_group = QGroupBox("📈 实时脑电波形")
        eeg_layout = QVBoxLayout(eeg_group)
        eeg_layout.setContentsMargins(6, 6, 6, 6)

        # Channel selector row
        ch_sel_row = QHBoxLayout()
        ch_sel_row.addWidget(QLabel("显示通道:"))
        self._eeg_ch_combo = QComboBox()
        self._eeg_ch_combo.setFixedWidth(120)
        self._eeg_ch_combo.addItems(["手+脚 (8ch)", "眨眼 (4ch)", "全部活跃"])
        self._eeg_ch_combo.currentIndexChanged.connect(self._on_eeg_ch_mode_changed)
        ch_sel_row.addWidget(self._eeg_ch_combo)

        # Time window selector
        ch_sel_row.addWidget(QLabel("  时窗:"))
        self._eeg_time_combo = QComboBox()
        self._eeg_time_combo.setFixedWidth(70)
        self._eeg_time_combo.addItems(["3s", "5s", "10s"])
        self._eeg_time_combo.setCurrentIndex(1)
        self._eeg_time_combo.currentIndexChanged.connect(self._on_eeg_time_changed)
        ch_sel_row.addWidget(self._eeg_time_combo)

        ch_sel_row.addStretch()
        eeg_layout.addLayout(ch_sel_row)

        # The waveform widget itself (v2.4.12: enlarged for 8-channel visibility)
        self._eeg_wave = EEGWaveWidget(sample_rate=SAMPLE_RATE, buf_seconds=10.0)
        self._eeg_wave.setMinimumHeight(400)
        self._eeg_wave.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        eeg_layout.addWidget(self._eeg_wave)

        # Store ch_defs for mode switching
        self._eeg_ch_defs = ch_defs

        layout.addWidget(eeg_group)

        # v2.4.12: Default to all active channels (8 ch) for full view
        self._eeg_ch_combo.setCurrentIndex(2)
        self._apply_eeg_ch_mode(2)

        # ── Control buttons ──
        ctrl_group = QGroupBox("控制")
        ctrl_layout = QHBoxLayout(ctrl_group)

        self._start_btn = QPushButton("▶  开始 BCI 控制")
        self._start_btn.setObjectName("startBtn")
        self._start_btn.setEnabled(False)
        self._start_btn.setMinimumHeight(44)
        self._start_btn.setToolTip(
            "启动脑机接口鼠标控制\n"
            "前提：需先连接设备并启动数据流"
        )
        self._start_btn.clicked.connect(self._start_bci)
        ctrl_layout.addWidget(self._start_btn)

        self._stop_btn = QPushButton("⏹  取消控制")
        self._stop_btn.setObjectName("stopBtn")
        self._stop_btn.setEnabled(False)
        self._stop_btn.setMinimumHeight(44)
        self._stop_btn.clicked.connect(self._request_stop_bci)
        ctrl_layout.addWidget(self._stop_btn)

        layout.addWidget(ctrl_group)

        # ── v2.4.12: Focus meter moved to bottom ──
        focus_group = QGroupBox("专注度 / 鼠标速度")
        focus_layout = QVBoxLayout(focus_group)
        self._focus_meter = FocusMeter()
        focus_layout.addWidget(self._focus_meter)
        self._speed_label = QLabel("当前速度: 8 px/tick")
        self._speed_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._speed_label.setStyleSheet("color: #8b949e; font-size: 11px;")
        focus_layout.addWidget(self._speed_label)
        layout.addWidget(focus_group)

        layout.addStretch()
        return panel

    def _make_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ── v2.2.2: Device status ──
        dev_group = QGroupBox("📡 设备状态")
        dev_layout = QVBoxLayout(dev_group)
        self._device_status = DeviceStatusWidget()
        dev_layout.addWidget(self._device_status)
        layout.addWidget(dev_group)

        # ── v2.0: Self-test panel ──
        test_group = QGroupBox("🖱️ 系统自检")
        test_layout = QHBoxLayout(test_group)

        # Left: test button + visual
        test_visual_layout = QVBoxLayout()
        self._test_visual = SelfTestVisual()
        test_visual_layout.addWidget(self._test_visual, alignment=Qt.AlignmentFlag.AlignCenter)
        test_layout.addLayout(test_visual_layout)

        # Right: controls + info
        test_ctrl_layout = QVBoxLayout()
        test_ctrl_layout.setSpacing(8)

        self._test_btn = QPushButton("▶  开始自检测试")
        self._test_btn.setObjectName("testBtn")
        self._test_btn.setMinimumHeight(36)
        self._test_btn.clicked.connect(self._start_self_test)
        test_ctrl_layout.addWidget(self._test_btn)

        test_info = QLabel(
            "鼠标将移动到屏幕正中央，\n"
            "快速画出屏幕大小十字，\n"
            "然后恢复到原点。"
        )
        test_info.setWordWrap(True)
        test_info.setStyleSheet("color: #8b949e; font-size: 11px; line-height: 1.6;")
        test_ctrl_layout.addWidget(test_info)

        self._test_result_label = QLabel("")
        self._test_result_label.setWordWrap(True)
        self._test_result_label.setStyleSheet(
            "color: #56d364; font-size: 11px; font-weight: bold;")
        test_ctrl_layout.addWidget(self._test_result_label)

        test_ctrl_layout.addStretch()
        test_layout.addLayout(test_ctrl_layout)
        layout.addWidget(test_group)

        # ── v2.2: Hand+Foot MI diagram ──
        mi_group = QGroupBox("🧠 手部+脚部运动想象 (v2.2)")
        mi_layout = QVBoxLayout(mi_group)

        mi_info = QLabel(
            "<b>8头顶通道 手+脚 连续向量控制：</b><br><br>"
            "<b>📐 通道布局：</b><br>"
            "<span style='color:#ff7043;'>● 左半球：</span>"
            "CH4(Aux手) <b>CH5(★手L)</b> <b>CH6(★脚L)</b> CH7(Aux脚)<br>"
            "<span style='color:#42a5f5;'>● 右半球：</span>"
            "CH12(Aux手) <b>CH13(★手R)</b> <b>CH14(★脚R)</b> CH15(Aux脚)<br>"
            "<br><b>🎯 运动想象映射：</b><br>"
            "<table>"
            "<tr><td>左手MI → 右侧ERD</td><td>→</td>"
            "<td style='color:#ff7043;'>⬅ 左移</td></tr>"
            "<tr><td>右手MI → 左侧ERD</td><td>→</td>"
            "<td style='color:#42a5f5;'>➡ 右移</td></tr>"
            "<tr><td>左脚MI → 右侧ERD</td><td>→</td>"
            "<td style='color:#66bb6a;'>⬇ 下移</td></tr>"
            "<tr><td>右脚MI → 左侧ERD</td><td>→</td>"
            "<td style='color:#ab47bc;'>⬆ 上移</td></tr>"
            "</table>"
            "<br><b>🔀 对角方向（手+脚同时想象）：</b><br>"
            "↖ 左上 = 左手 + 右脚 &nbsp;&nbsp;"
            "↗ 右上 = 右手 + 右脚<br>"
            "↙ 左下 = 左手 + 左脚 &nbsp;&nbsp;"
            "↘ 右下 = 右手 + 左脚<br>"
            "<br><b>✨ 新特性：</b> "
            "连续向量输出 → 鼠标平滑移动 → 流畅操控感"
        )
        mi_info.setWordWrap(True)
        mi_info.setStyleSheet(
            "color: #c9d1d9; font-size: 11px; line-height: 1.7;"
            "background: transparent;"
        )
        mi_layout.addWidget(mi_info)
        layout.addWidget(mi_group)

        # ── Log console ──
        log_group = QGroupBox("系统日志")
        log_layout = QVBoxLayout(log_group)
        self._log_area = QTextEdit()
        self._log_area.setReadOnly(True)
        self._log_area.setMaximumHeight(200)
        self._log_area.setStyleSheet(
            "QTextEdit { background: #0d1117; color: #8b949e; "
            "font-family: Monospace; font-size: 11px; "
            "border: 1px solid #21262d; border-radius: 4px; }"
        )
        log_layout.addWidget(self._log_area)

        clear_btn = QPushButton("清空日志")
        clear_btn.setFixedHeight(26)
        clear_btn.clicked.connect(self._log_area.clear)
        log_layout.addWidget(clear_btn)
        layout.addWidget(log_group, 1)

        # ── Threshold sliders ──
        thresh_group = QGroupBox("眨眼阈值调节")
        thresh_layout = QGridLayout(thresh_group)

        thresh_layout.addWidget(QLabel("普通眨眼阈值 (µV):"), 0, 0)
        self._low_thresh_slider = QSlider(Qt.Orientation.Horizontal)
        self._low_thresh_slider.setRange(20, 300)
        self._low_thresh_slider.setValue(80)
        self._low_thresh_label = QLabel("80 µV")
        self._low_thresh_slider.valueChanged.connect(
            lambda v: (self._low_thresh_label.setText(f"{v} µV"),
                       self._update_thresholds())
        )
        thresh_layout.addWidget(self._low_thresh_slider, 0, 1)
        thresh_layout.addWidget(self._low_thresh_label, 0, 2)

        thresh_layout.addWidget(QLabel("有意眨眼阈值 (µV):"), 1, 0)
        self._high_thresh_slider = QSlider(Qt.Orientation.Horizontal)
        self._high_thresh_slider.setRange(50, 500)
        self._high_thresh_slider.setValue(150)
        self._high_thresh_label = QLabel("150 µV")
        self._high_thresh_slider.valueChanged.connect(
            lambda v: (self._high_thresh_label.setText(f"{v} µV"),
                       self._update_thresholds())
        )
        thresh_layout.addWidget(self._high_thresh_slider, 1, 1)
        thresh_layout.addWidget(self._high_thresh_label, 1, 2)

        layout.addWidget(thresh_group)
        return panel

    def _apply_stylesheet(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #0d1117;
                color: #c9d1d9;
            }
            QGroupBox {
                border: 1px solid #30363d;
                border-radius: 6px;
                margin-top: 8px;
                font-size: 12px;
                font-weight: bold;
                color: #58a6ff;
                padding: 6px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 6px;
            }
            QLabel { color: #c9d1d9; }
            QComboBox {
                background: #161b22;
                border: 1px solid #30363d;
                border-radius: 4px;
                padding: 4px 8px;
                color: #c9d1d9;
            }
            QPushButton {
                background: #21262d;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 6px 12px;
                color: #c9d1d9;
                font-size: 12px;
            }
            QPushButton:hover { background: #30363d; border-color: #58a6ff; }
            QPushButton:disabled { color: #484f58; border-color: #21262d; }
            QPushButton#connectBtn {
                background: #1f6feb; color: white; border-color: #1f6feb;
            }
            QPushButton#connectBtn:hover { background: #388bfd; }
            QPushButton#btScanBtn {
                background: #6e40c9; color: white; border-color: #6e40c9;
            }
            QPushButton#btScanBtn:hover { background: #7c4dff; }
            QPushButton#btConnectBtn {
                background: #1f6feb; color: white; border-color: #1f6feb;
            }
            QPushButton#btConnectBtn:hover { background: #388bfd; }
            QPushButton#btConnectBtn:disabled {
                background: #21262d; color: #484f58; border-color: #30363d;
            }
            QPushButton#testBtn {
                background: #bf5b00; color: white; border-color: #ffab00;
                font-size: 14px; font-weight: bold;
            }
            QPushButton#testBtn:hover { background: #ff8c00; }
            QPushButton#testBtn:disabled {
                background: #21262d; color: #484f58; border-color: #30363d;
            }
            QPushButton#startBtn {
                background: #238636; color: white; border-color: #2ea043;
                font-size: 14px; font-weight: bold;
            }
            QPushButton#startBtn:hover { background: #2ea043; }
            QPushButton#stopBtn {
                background: #b62324; color: white; border-color: #da3633;
                font-size: 14px; font-weight: bold;
            }
            QPushButton#stopBtn:hover { background: #da3633; }
            QCheckBox { color: #8b949e; }
            QSlider::groove:horizontal {
                height: 4px; background: #30363d; border-radius: 2px;
            }
            QSlider::handle:horizontal {
                width: 12px; height: 12px; margin: -4px 0;
                background: #58a6ff; border-radius: 6px;
            }
            QFrame#titleBar {
                background: #161b22;
                border-bottom: 1px solid #30363d;
                border-radius: 6px;
            }
            QSplitter::handle { background: #30363d; width: 1px; }
            QStatusBar { color: #8b949e; font-size: 11px; }
        """)

    # ── v2.0: Bluetooth ─────────────────────────────────────────────────────

    def _scan_bluetooth(self):
        """Start Bluetooth device scan in background thread."""
        self._bt_scan_btn.setEnabled(False)
        self._bt_scan_btn.setText("🔍 搜索中...")
        self._bt_status_label.setText("正在搜索蓝牙设备...")
        self._bt_status_label.setStyleSheet("color: #ffab00; font-size: 11px;")
        self._bt_device_list.clear()
        self._bt_connect_btn.setEnabled(False)

        self._append_log("📡 开始搜索蓝牙设备...")

        def scan_thread():
            devices = self.streamer.scan_bluetooth()
            self.signals.bt_scan_finished.emit(devices)

        thread = threading.Thread(target=scan_thread, daemon=True)
        thread.start()

    @pyqtSlot(list)
    def _on_bt_scan_finished(self, devices: list):
        """Handle Bluetooth scan results."""
        self._bt_devices = devices
        self._bt_scan_btn.setEnabled(True)
        self._bt_scan_btn.setText("🔍 搜索蓝牙设备")
        self._bt_device_list.clear()

        if not devices:
            self._bt_status_label.setText("未发现蓝牙设备 — 请确认 BLED112 已插入")
            self._bt_status_label.setStyleSheet("color: #f85149; font-size: 11px;")
            self._append_log("📡 未发现蓝牙设备")
            return

        self._bt_status_label.setText(f"发现 {len(devices)} 个设备，请选择后连接")
        self._bt_status_label.setStyleSheet("color: #56d364; font-size: 11px;")
        self._bt_connect_btn.setEnabled(True)

        for dev in devices:
            name = dev.get('name', 'Unknown')
            dev_type = dev.get('type', '?')
            port = dev.get('port', '')
            address = dev.get('address', '')
            item_text = f"{name}"
            if address:
                item_text += f"  [{address}]"
            item_text += f"  ({dev_type})"
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, dev)
            self._bt_device_list.addItem(item)

        self._bt_device_list.setCurrentRow(0)
        self._append_log(f"📡 发现 {len(devices)} 个蓝牙设备")

    def _connect_bluetooth(self):
        """Connect to selected Bluetooth device."""
        current_item = self._bt_device_list.currentItem()
        if not current_item:
            self._bt_status_label.setText("请先选择一个设备")
            return

        device = current_item.data(Qt.ItemDataRole.UserRole)
        self._bt_connect_btn.setEnabled(False)
        self._bt_connect_btn.setText("📡 连接中...")

        def connect_thread():
            ok = self.streamer.connect_bluetooth(device)
            if ok:
                self.signals.board_status_changed.emit(
                    "connected", f"蓝牙: {device.get('name', '?')}"
                )
            else:
                self.signals.log_message.emit("❌ 蓝牙连接失败")

        thread = threading.Thread(target=connect_thread, daemon=True)
        thread.start()

    # ── v2.0: Self-test ─────────────────────────────────────────────────────

    def _start_self_test(self):
        """Run mouse self-test: draw screen-sized cross and return."""
        if self._test_running:
            return

        self._test_running = True
        self._test_btn.setEnabled(False)
        self._test_btn.setText("⏳ 测试中...")
        self._test_result_label.setText("")
        self._test_visual.reset()
        self._test_visual.set_progress(0)
        self._test_visual.update()
        self._append_log("🖱️ 开始鼠标自检 — 屏幕中心画十字")

        def test_thread():
            result = self.mouse_ctrl.self_test(
                status_callback=lambda s, d: self.signals.test_status.emit(s, d)
            )
            self._test_running = False

        thread = threading.Thread(target=test_thread, daemon=True)
        thread.start()

    @pyqtSlot(str, object)
    def _on_test_status(self, status: str, data):
        """Handle self-test status updates."""
        if status == "progress":
            step = data.get("step", 0)
            phase = data.get("phase", "")
            self._test_visual.set_progress(step)
            phase_names = {
                "center": "📍 移动到中心",
                "horizontal": "➖ 水平线",
                "vertical": "| 竖直线",
                "return": "↩ 恢复原点",
            }
            name = phase_names.get(phase, phase)
            total = data.get("total_steps", 3)
            self._append_log(f"  🖱️ 自检进度: {step}/{total} {name}")
        elif status == "complete":
            origin = data.get("origin", (0, 0))
            center = data.get("center", (0, 0))
            screen = data.get("screen", (0, 0))
            self._test_visual.set_progress(3)
            self._test_btn.setEnabled(True)
            self._test_btn.setText("▶  开始自检测试")
            status_text = (f"✅ 测试通过！十字绘制完成。\n"
                           f"屏幕: {screen[0]}×{screen[1]}  |  中心: ({center[0]},{center[1]})")
            self._test_result_label.setText(status_text)
            self._test_result_label.setStyleSheet(
                "color: #56d364; font-size: 11px; font-weight: bold;")
            self._append_log(f"✅ 自检完成 — 已回到原点({origin[0]:.0f},{origin[1]:.0f})")
        elif status == "error":
            msg = data.get("message", "未知错误")
            self._test_visual.reset()
            self._test_btn.setEnabled(True)
            self._test_btn.setText("▶  开始自检测试")
            self._test_result_label.setText(f"❌ 测试失败: {msg}")
            self._test_result_label.setStyleSheet(
                "color: #f85149; font-size: 11px; font-weight: bold;")
            self._append_log(f"❌ 自检失败: {msg}")

    # ── Signal handlers ────────────────────────────────────────────────────

    @pyqtSlot(dict)
    def _update_channel_leds(self, status: dict):
        for i, info in status.items():
            led = self._ch_leds.get(i)
            if led is None:
                continue
            if info["disabled"]:
                led.set_state("disabled")
            elif info["active"]:
                led.set_state("active")
            else:
                led.set_state("inactive")

    @pyqtSlot(float)
    def _update_focus(self, score: float):
        self._focus_meter.set_score(score)
        from signal_processor import FOCUS_MIN_SPEED, FOCUS_MAX_SPEED
        speed = FOCUS_MIN_SPEED + score * (FOCUS_MAX_SPEED - FOCUS_MIN_SPEED)
        self._speed_label.setText(f"当前速度: {speed:.1f} px/tick")
        self.mouse_ctrl.set_focus(score)

    # ── v2.4.12: Removed blink/mi GUI event handlers (实时事件模块已删除) ───

    # ── v2.2: Continuous vector control (smooth movement) ──────────────────

    def _on_vector(self, dx: float, dy: float, label: Optional[str]):
        """
        v2.2/v2.4.12: Continuous 2D direction vector from MI detector.

        Called at MI_STEP_SEC intervals (~100ms) with the current
        direction vector. The mouse controller smooths this for fluid
        cursor movement.

        v2.4.12: Removed visual-feedback label updates (MI label gone).
        """
        if self._bci_active:
            self.mouse_ctrl.set_vector(dx, dy)

    @pyqtSlot(str, str)
    def _on_board_status(self, status: str, detail: str):
        # v2.3.3: Throttle "error" signals — transient BrainFlow errors like
        # INVALID_ARGUMENTS_ERROR:13 "unable to obtain buffer size" should NOT
        # repeatedly reset the connection state and re-trigger device detection.
        # Only act on "error" if it has been >5 s since the last one, OR the
        # error detail has changed (different underlying cause).
        if status == "error":
            now = time.time()
            if now - self._last_error_time < 5.0:
                # Suppress repeated same-type error; just log it quietly
                self._append_log(f"[板子] 短暂错误(已抑制): {detail}")
                return
            self._last_error_time = now

        msg = f"[板子] {status}"
        if detail:
            msg += f": {detail}"
        self._append_log(msg)
        self._statusbar.showMessage(msg)

        if status == "connected":
            self._connected = True
            self._last_error_time = 0.0    # reset error timer on successful connect
            self._connect_btn.setText("🔌 断开")
            self._bt_connect_btn.setText("📡 已连接 ✓")
            self._bt_connect_btn.setEnabled(False)
            self._start_btn.setEnabled(True)
            self._device_dialog_shown = False  # reset dialog guard
        elif status in ("disconnected", "error"):
            self._connected = False
            self._streaming = False            # v2.2.5-fix: keep in sync
            self._connect_btn.setText("🔌 连接")
            self._bt_connect_btn.setText("📡 连接蓝牙设备")
            self._bt_connect_btn.setEnabled(bool(self._bt_devices))
            self._start_btn.setEnabled(False)
            self._detected_device = None       # allow re-detection
            self._device_dialog_shown = False
            # Reset data tracking so UI doesn't show stale SPS
            self._data_count = 0
            self._data_t0 = 0.0
            self._sps_counter = 0
            self._current_sps = 0.0

    @pyqtSlot()
    def _on_all_channels_ready(self):
        self._append_log("✅ 所有活跃通道均有信号 — 自动激活 BCI 控制")
        self._statusbar.showMessage("所有通道就绪 — BCI控制已激活")
        if not self._bci_active:
            self._activate_bci()

    # ── v2.4.10: Keyboard exit — Esc (single) + Space (double) ──────────────

    def _start_space_listener(self):
        """Start global keyboard listener for exit keys.

        v2.4.10: Esc (single press) or double-space exits BCI.
        Uses PyQt signal for thread-safe cross-thread communication."""
        if not _SPACE_LISTENER_AVAILABLE or self._space_listener is not None:
            return
        try:
            self._last_space_time = 0.0
            self._space_listener = _KeyboardListener(on_press=self._on_key_press)
            self._space_listener.start()
            logger.info("Global keyboard exit listener started (Esc / double-Space)")
        except Exception as exc:
            logger.warning(f"Failed to start keyboard listener: {exc}")

    def _stop_space_listener(self):
        """Stop the global keyboard listener."""
        if self._space_listener is not None:
            try:
                self._space_listener.stop()
            except Exception:
                pass
            self._space_listener = None
            logger.info("Global keyboard exit listener stopped")

    def _on_key_press(self, key):
        """
        v2.4.10: pynput keyboard callback — runs in listener thread.

        Esc (single press immediately) or double-space (500ms window) → exit.
        Emits PyQt signal for thread-safe delivery to main thread.
        """
        try:
            # ── Esc detected ──────────────────────────────────────────
            is_esc = (
                (hasattr(key, 'name') and key.name == 'esc') or
                (key == _Key.esc if _Key else False)
            )
            if is_esc and self._bci_active:
                logger.info("Esc detected via global listener — requesting exit")
                self.signals.keyboard_exit_requested.emit()
                return

            # ── Space detected ────────────────────────────────────────
            is_space = (
                (hasattr(key, 'name') and key.name == 'space') or
                (hasattr(key, 'char') and key.char == ' ') or
                (key == _Key.space if _Key else False)
            )
            if is_space and self._bci_active:
                now = time.time()
                if now - self._last_space_time < 0.5:
                    logger.info("Double-space detected — requesting exit")
                    self.signals.keyboard_exit_requested.emit()
                self._last_space_time = now
        except Exception as exc:
            logger.debug(f"Keyboard listener callback error: {exc}")

    # ── Actions ────────────────────────────────────────────────────────────

    # ── v2.2.1: Auto device detection ────────────────────────────────────

    def _check_for_device(self):
        """
        Periodically scan for OpenBCI dongle.

        When a board is detected (not already connected), pop up
        the "设备接入，是否连接？" confirmation dialog. On confirm,
        auto-connect and start streaming.
        """
        # Skip if already connected, or if dialog is currently shown
        if self._connected or self._device_dialog_shown:
            return

        # Don't scan if user is already manually interacting
        if self._test_running:
            return

        try:
            device = self.streamer.detect_openbci_device()
        except Exception:
            return

        if device is None:
            # No device found — reset state so we can detect next time
            self._detected_device = None
            return

        # Same device as last time? Skip
        if self._detected_device is not None:
            if self._detected_device.get('port') == device.get('port'):
                return

        # New device detected!
        self._detected_device = device
        self._device_dialog_shown = True

        self._append_log(f"🔍 检测到设备: {device.get('name', '?')} "
                         f"({device.get('port', '?')})")
        self._statusbar.showMessage(
            f"检测到设备: {device.get('name', '?')} — 等待确认连接..."
        )

        # Show dialog
        dialog = DeviceDetectedDialog(device, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # User confirmed — auto connect
            # Keep _device_dialog_shown=True until connected (set in _on_board_status)
            # to prevent re-detection during background connection
            self._auto_connect_device(device)
        else:
            self._append_log("⏭  用户选择暂不连接，将继续监控设备...")
            self._statusbar.showMessage("就绪 — 等待设备接入...")
            self._detected_device = None       # allow re-detection next time
            self._device_dialog_shown = False

    def _auto_connect_device(self, device: dict):
        """
        Auto-connect to detected device.

        Sets up serial port, connects via BrainFlow, starts streaming.
        After streaming starts, the user can click "开始 BCI 控制".
        """
        port = device.get('port', '')
        name = device.get('name', 'OpenBCI')
        self._append_log(f"✅ 用户确认连接 — {name} ({port})")

        # Select the port in combo
        idx = self._port_combo.findText(port)
        if idx >= 0:
            self._port_combo.setCurrentIndex(idx)
        else:
            self._port_combo.insertItem(0, port)
            self._port_combo.setCurrentIndex(0)

        # Disable simulation mode for real hardware
        self._sim_check.setChecked(False)
        self.streamer._sim_mode = False

        # Connect in background thread
        def connect_thread():
            self.signals.log_message.emit(f"⏳ 正在连接 {name}...")
            ok = self.streamer.connect(port)
            if ok:
                self.signals.board_status_changed.emit(
                    "connected", f"自动连接: {name} ({port})"
                )
                self.signals.log_message.emit("⏳ 正在启动数据流...")
                started = self.streamer.start_streaming()
                if started:
                    self._streaming = True
                    self.signals.log_message.emit("✅ 设备已就绪 — 请点击「开始 BCI 控制」")
                else:
                    self.signals.log_message.emit("❌ 自动启动数据流失败，请手动操作")
                    # Allow re-detection on stream start failure
                    self._detected_device = None
                    self._device_dialog_shown = False
            else:
                self.signals.log_message.emit(
                    f"❌ 自动连接失败: {name}，请检查设备是否已开机"
                )
                # Allow re-detection on connect failure
                self._detected_device = None
                self._device_dialog_shown = False

        thread = threading.Thread(target=connect_thread, daemon=True)
        thread.start()

    def _refresh_ports(self):
        self._port_combo.clear()
        try:
            ports = self.streamer.get_available_ports()
            for p in ports:
                self._port_combo.addItem(p)
            if not ports:
                self._port_combo.addItem("(无可用串口)")
        except Exception:
            self._port_combo.addItem("(串口列表获取失败)")
        self._port_combo.addItem("SIMULATION")

    def _toggle_connect(self):
        if not self._connected:
            port = self._port_combo.currentText()
            sim = self._sim_check.isChecked() or port == "SIMULATION"

            if sim:
                self.streamer._sim_mode = True
                self.signals.board_status_changed.emit("connected", "SIMULATION MODE")
            else:
                self.streamer._sim_mode = False
                ok = self.streamer.connect(port)
                if not ok:
                    return

            self._append_log("⏳ 正在启动数据流...")
            started = self.streamer.start_streaming()
            if not started:
                self._append_log("❌ 数据流启动失败，请重试")
                QMessageBox.warning(
                    self, "启动失败",
                    "数据流启动失败。\n\n"
                    "可能原因：\n"
                    "• 设备连接异常\n"
                    "• BrainFlow 驱动问题\n\n"
                    "请断开后重新连接。"
                )
                return
            self._streaming = True
        else:
            self._stop_all()

    def _start_bci(self):
        if not self._streaming:
            QMessageBox.warning(
                self, "未连接设备",
                "请先连接您的设备后再尝试一次"
            )
            return
        self._activate_bci()

    def _activate_bci(self):
        if self._bci_active:
            return

        # v2.2.5-fix: validate mouse control backend is available
        from mouse_controller import _MOUSE_OK
        if not _MOUSE_OK:
            QMessageBox.critical(
                self, "控制模块不可用",
                "鼠标控制模块初始化失败。\n\n"
                "• macOS: 请确认已在「系统偏好设置 → 隐私与安全性 → 辅助功能」中添加本应用。\n"
                "• 其他系统: 请确认 pynput 已正确安装。"
            )
            self._append_log("❌ 鼠标控制模块不可用，无法启动 BCI 控制")
            return

        try:
            self.mouse_ctrl.start()
        except Exception as exc:
            QMessageBox.critical(
                self, "启动控制失败",
                f"鼠标控制启动失败：{exc}\n\n"
                "请检查：\n"
                "• macOS: 辅助功能权限是否已授予\n"
                "• 是否有其他程序独占鼠标控制\n"
                "• 重启应用后重试"
            )
            self._append_log(f"❌ BCI 控制启动失败: {exc}")
            return

        self._bci_active = True
        self._start_btn.setEnabled(False)
        self._start_btn.setText("🔄 控制中...")
        self._start_btn.setStyleSheet(
            "QPushButton {"
            "background: #30363d; color: #8b949e;"
            "border: 1px solid #21262d;"
            "font-size: 14px; font-weight: bold;"
            "}"
        )
        self._stop_btn.setEnabled(True)
        self._statusbar.showMessage("🟢 BCI 鼠标控制已激活 | 按 Esc 键退出")
        self._append_log("🟢 BCI 控制已开始 — 鼠标已被接管 (按 Esc 键退出)")
        self.setWindowTitle(f"🧠 HybridMI-BCI GUI — ● 控制中 (Esc 退出)  v{APP_VERSION}")
        # v2.4.10: global keyboard listener for Esc / double-Space exit
        self._start_space_listener()

    def _request_stop_bci(self):
        # v2.4.9: Direct stop — no modal dialog that could freeze when
        # the control loop was starving the Qt event loop of CPU time.
        # The old CancelControlDialog could become unreachable if the
        # GUI was sluggish due to GIL contention.
        self._append_log("⏸  用户请求停止 BCI 控制…")
        self._stop_bci()

    def _stop_bci(self):
        if not self._bci_active:
            return
        # v2.4.8: Set flag FIRST so control loop sees it immediately
        self._bci_active = False
        # v2.4.3: stop global space listener (before mouse controller)
        self._stop_space_listener()
        # Stop mouse controller (sets _active=False + _stop_event, joins thread)
        self.mouse_ctrl.stop()
        self._start_btn.setEnabled(True)
        self._start_btn.setText("▶  开始 BCI 控制")
        self._start_btn.setStyleSheet("")  # restore default green stylesheet
        self._stop_btn.setEnabled(False)
        self._statusbar.showMessage("⬜ BCI 控制已取消 — 手动模式")
        self._append_log("⬜ BCI 控制已取消，鼠标恢复手动控制")
        self.setWindowTitle(f"HybridMI-BCI GUI  v{APP_VERSION}")

    def _stop_all(self):
        self._stop_bci()
        self.streamer.stop_streaming()
        self.streamer.disconnect()
        self._streaming = False
        self._connected = False
        self._connect_btn.setText("🔌 连接")
        self._bt_connect_btn.setText("📡 连接蓝牙设备")
        self._bt_connect_btn.setEnabled(bool(self._bt_devices))
        self._start_btn.setEnabled(False)
        self._detected_device = None
        self._device_dialog_shown = False
        # Reset data tracking
        self._data_count = 0
        self._data_t0 = 0.0
        self._data_last_t = 0.0
        self._sps_counter = 0
        self._sps_window_start = 0.0
        self._sps_wall_counter = 0
        self._sps_wall_start = 0.0
        self._current_sps = 0.0
        self._data_heartbeat = 0        # v2.4.7
        self._last_on_data_err = ""     # v2.4.7
        # v2.4.8: reset DC baselines
        self._dc_baseline_ch5  = None
        self._dc_baseline_ch6  = None
        self._dc_baseline_ch13 = None
        self._dc_baseline_ch14 = None
        # v2.4.11: reset previous means for delta detection
        self._prev_ch5  = None
        self._prev_ch6  = None
        self._prev_ch13 = None
        self._prev_ch14 = None
        # v2.4.13: reset simulation state
        self._sim_dir = None
        self._sim_steps = 0
        # v2.4.14: reset waveform phase
        self._sim_wave_phase = 0.0
        self._device_status.set_connected(False)
        self._device_status.set_streaming(False)
        self._device_status.set_sps(0.0)
        self._device_status.set_total_samples(0)

    # ── v2.4.6: Raw EEG → mouse (CH5/13 → dx, CH6/14 → dy) ──────────────

    # EEG sample rate — matches BrainFlow Cyton+Daisy board
    _EEG_FS: int = 250

    def _init_notch_filters(self):
        """Pre-design 50 Hz + 60 Hz IIR notch filters (SOS form)."""
        try:
            Q = 30.0
            nyq = 0.5 * self._EEG_FS
            # 50 Hz notch
            b_50, a_50 = sp_signal.iirnotch(50.0 / nyq, Q)
            self._notch_sos_50 = sp_signal.tf2sos(b_50, a_50)
            # 60 Hz notch
            b_60, a_60 = sp_signal.iirnotch(60.0 / nyq, Q)
            self._notch_sos_60 = sp_signal.tf2sos(b_60, a_60)
            logger.info("Notch filters 50+60 Hz initialized (SOS, Q=30, fs=250)")
        except Exception as exc:
            logger.warning(f"Failed to init notch filters: {exc}")
            self._notch_sos_50 = None
            self._notch_sos_60 = None

    def _apply_notch(self, signal_1d: np.ndarray) -> np.ndarray:
        """Apply 50+60 Hz notch filters to a 1-D signal array using sosfiltfilt."""
        try:
            if self._notch_sos_50 is not None and len(signal_1d) > 10:
                signal_1d = sp_signal.sosfiltfilt(self._notch_sos_50, signal_1d)
            if self._notch_sos_60 is not None and len(signal_1d) > 10:
                signal_1d = sp_signal.sosfiltfilt(self._notch_sos_60, signal_1d)
            return signal_1d
        except Exception:
            return signal_1d  # fallback: pass through unfiltered

    def _sim_mouse_control(self):
        """v2.4.13: Random 8-direction movement for simulation mode.

        Picks a random direction (up/down/left/right + diagonals),
        moves in that direction for a random number of steps
        (≈ 0.5–2.5 s at ~20 Hz), then picks a new direction.

        This gives a natural "exploring" feel — the cursor walks
        around the screen like a user testing the BCI without hardware.
        """
        import random

        # Check if current direction is exhausted
        if self._sim_steps <= 0:
            directions = [
                (0.0, -1.0),   # up
                (0.0,  1.0),   # down
                (-1.0, 0.0),   # left
                (1.0,  0.0),   # right
                (-1.0, -1.0),  # up-left
                (1.0, -1.0),   # up-right
                (-1.0, 1.0),   # down-left
                (1.0,  1.0),   # down-right
            ]
            self._sim_dir = random.choice(directions)
            # Random duration: 10-50 steps ≈ 0.5-2.5 seconds at ~20 Hz
            self._sim_steps = random.randint(10, 50)

        dx, dy = self._sim_dir
        self.mouse_ctrl.set_vector(dx, dy)
        self._sim_steps -= 1

    def _generate_sim_waveform(self, n_samples: int) -> np.ndarray:
        """v2.4.14: Synthetic EEG data that visually reflects simulation direction.

        Generates 16-channel data where CH5/6/13/14 show sinusoidal activity
        proportional to the current movement direction vector:
          - RIGHT (dx>0):  CH5  ↑  (hand L active)
          - LEFT  (dx<0):  CH13 ↑  (hand R active)
          - DOWN  (dy>0):  CH6  ↑  (foot L active)
          - UP    (dy<0):  CH14 ↑  (foot R active)
          - Diagonals:     combination of hand + foot channels

        Other channels carry low-level random noise to look realistic.
        Uses ~10 Hz alpha-band sinusoids — typical MI rhythm.
        """
        import random

        data = np.zeros((16, n_samples), dtype=np.float64)

        # Base noise floor for all channels (≈ ±5 µV)
        for ch in range(16):
            data[ch, :] = np.random.randn(n_samples).astype(np.float64) * 5.0

        # If no active direction, return just noise
        if self._sim_dir is None:
            return data

        dx, dy = self._sim_dir

        # Time vector with phase continuity across calls
        fs = 250.0
        t = np.arange(n_samples, dtype=np.float64) / fs + self._sim_wave_phase
        self._sim_wave_phase = (self._sim_wave_phase + n_samples / fs) % (2.0 * np.pi)

        amp = 60.0   # µV peak amplitude for active channels

        # ── Hand channels (horizontal axis) ──────────────────────────
        # CH5  (idx 4): hand L — active when moving RIGHT (dx > 0)
        if dx > 0:
            data[4, :] += amp * dx * np.sin(2.0 * np.pi * 10.0 * t)
        # CH13 (idx 12): hand R — active when moving LEFT (dx < 0)
        if dx < 0:
            data[12, :] += amp * abs(dx) * np.sin(2.0 * np.pi * 11.0 * t)

        # ── Foot channels (vertical axis) ────────────────────────────
        # CH6  (idx 5): foot L — active when moving DOWN (dy > 0)
        if dy > 0:
            data[5, :] += amp * dy * np.sin(2.0 * np.pi * 12.0 * t)
        # CH14 (idx 13): foot R — active when moving UP (dy < 0)
        if dy < 0:
            data[13, :] += amp * abs(dy) * np.sin(2.0 * np.pi * 13.0 * t)

        return data

    def _raw_eeg_control(self, data: np.ndarray, timestamp: float):
        """Map raw EEG changes to mouse dx/dy — delta-based, no bandpass.

        CH5  (idx 4)  vs CH13 (idx 12) → horizontal dx
        CH6  (idx 5)  vs CH14 (idx 13) → vertical   dy

        Only 50+60 Hz notch filtering applied (power-line removal).

        v2.4.11: CHANGE-DETECTION mode.  Instead of using the absolute
        deviation from a baseline (which can cause sustained drift if the
        user holds a thought), we track the *delta* — the difference between
        the current filtered mean and the previous one.  Only when the delta
        exceeds a threshold does the mouse move.  When the EEG is stable
        (no change), set_vector(0, 0) stops the cursor.

        This is a first-difference (derivative) approach:
          - Static DC offsets → differentiated to zero
          - Slow drift → below threshold, ignored
          - Rapid MI-related changes → trigger movement

        CH5 ↑ + CH13 ↓  →  RIGHT (dx > 0)
        CH13 ↑ + CH5 ↓  →  LEFT  (dx < 0)
        CH6 ↑ + CH14 ↓  →  DOWN  (dy > 0)
        CH14 ↑ + CH6 ↓  →  UP    (dy < 0)
        """
        try:
            n_ch, n_samp = data.shape[0], data.shape[1]
            if n_ch < 16 or n_samp == 0:
                return

            # ── Apply notch (50+60 Hz) to the 4 control channels ─────────
            ch5  = self._apply_notch(data[4, :].astype(np.float64))
            ch6  = self._apply_notch(data[5, :].astype(np.float64))
            ch13 = self._apply_notch(data[12, :].astype(np.float64))
            ch14 = self._apply_notch(data[13, :].astype(np.float64))

            ch5_mean  = float(np.mean(ch5))
            ch6_mean  = float(np.mean(ch6))
            ch13_mean = float(np.mean(ch13))
            ch14_mean = float(np.mean(ch14))

            # ── v2.4.11: Delta (change) detection ────────────────────────
            # First call: seed previous values, no movement
            if self._prev_ch5 is None:
                self._prev_ch5  = ch5_mean
                self._prev_ch6  = ch6_mean
                self._prev_ch13 = ch13_mean
                self._prev_ch14 = ch14_mean
                # Also seed DC baselines for logging
                self._dc_baseline_ch5  = ch5_mean
                self._dc_baseline_ch6  = ch6_mean
                self._dc_baseline_ch13 = ch13_mean
                self._dc_baseline_ch14 = ch14_mean
                return

            # ── Compute deltas (change since last sample) ─────────────────
            d5  = ch5_mean  - self._prev_ch5
            d6  = ch6_mean  - self._prev_ch6
            d13 = ch13_mean - self._prev_ch13
            d14 = ch14_mean - self._prev_ch14

            # Store current as previous for next call
            self._prev_ch5  = ch5_mean
            self._prev_ch6  = ch6_mean
            self._prev_ch13 = ch13_mean
            self._prev_ch14 = ch14_mean

            # ── Slowly update DC baselines (for logging only) ────────────
            alpha = 0.001
            self._dc_baseline_ch5  += (ch5_mean  - self._dc_baseline_ch5)  * alpha
            self._dc_baseline_ch6  += (ch6_mean  - self._dc_baseline_ch6)  * alpha
            self._dc_baseline_ch13 += (ch13_mean - self._dc_baseline_ch13) * alpha
            self._dc_baseline_ch14 += (ch14_mean - self._dc_baseline_ch14) * alpha

            # ── Differential delta → dx / dy ──────────────────────────────
            dx_raw = d5 - d13
            dy_raw = d6 - d14

            # ── Dead zone: ignore tiny changes (< 3 µV delta) ────────────
            DELTA_DEAD_ZONE = 3.0
            if abs(dx_raw) < DELTA_DEAD_ZONE:
                dx_raw = 0.0
            if abs(dy_raw) < DELTA_DEAD_ZONE:
                dy_raw = 0.0

            # Scale: ~30 µV delta → full deflection [-1, 1]
            dx = max(-1.0, min(1.0, dx_raw / 30.0))
            dy = max(-1.0, min(1.0, dy_raw / 30.0))

            # ── Apply to mouse controller ─────────────────────────────────
            self.mouse_ctrl.set_vector(dx, dy)

            # ── Periodic debug log (every ~60 calls ≈ 2 sec) ─────────────
            self._raw_control_call_count += 1
            if self._raw_control_call_count % 60 == 0:
                logger.info(
                    f"[RAW-CTRL] delta: d5={d5:.1f} d13={d13:.1f} → dx_raw={dx_raw:.1f} dx={dx:.2f} | "
                    f"d6={d6:.1f} d14={d14:.1f} → dy_raw={dy_raw:.1f} dy={dy:.2f} | "
                    f"baseline ch5={self._dc_baseline_ch5:.0f} ch13={self._dc_baseline_ch13:.0f}"
                )

        except Exception as exc:
            logger.warning(f"Raw EEG control error: {exc}")

    def _on_data(self, data: np.ndarray, timestamp: float):
        """Called from BrainFlow stream thread. MUST NOT crash."""
        # ── v2.4.7: SPS + sample tracking at TOP (before any processing) ──
        # This runs FIRST so even if processing crashes, SPS still shows up.
        try:
            n = data.shape[1] if hasattr(data, 'shape') and data.ndim >= 2 else 0
        except Exception:
            n = 0

        self._data_heartbeat += 1
        self._data_count += n

        # Wall-clock SPS (independent of BrainFlow timestamp)
        wall_now = time.time()
        if self._sps_wall_start == 0.0:
            self._sps_wall_start = wall_now
        self._sps_wall_counter += n
        elapsed = wall_now - self._sps_wall_start
        if elapsed >= 1.0:
            self._current_sps = self._sps_wall_counter / max(elapsed, 0.001)
            self._sps_wall_counter = 0
            self._sps_wall_start = wall_now

        # Periodic log to confirm data IS flowing
        if self._data_heartbeat % 60 == 0:
            logger.info(
                f"[DATA-FLOW] _on_data() called {self._data_heartbeat}x | "
                f"total samples={self._data_count} | "
                f"SPS={self._current_sps:.0f} | "
                f"shape={data.shape if hasattr(data,'shape') else 'N/A'}"
            )

        # ── Now run processing (wrapped — must never kill the stream) ─────
        try:
            self.processor.push_data(data, timestamp)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if err != self._last_on_data_err:
                self._last_on_data_err = err
                logger.warning(f"processor.push_data failed: {err}")

        try:
            if self._bci_active:
                if self.streamer._sim_mode:
                    self._sim_mouse_control()
                else:
                    self._raw_eeg_control(data, timestamp)
        except Exception as e:
            logger.debug(f"raw_eeg_control failed: {e}")

        try:
            if self._bci_active and self.streamer._sim_mode:
                sim_data = self._generate_sim_waveform(data.shape[1])
                self._eeg_wave.push_data(sim_data)
            else:
                self._eeg_wave.push_data(data)
        except Exception as e:
            logger.debug(f"eeg_wave.push_data failed: {e}")

    def _refresh_device_status(self):
        """Update device status widget once per second."""
        self._device_status.set_connected(self._connected)
        self._device_status.set_streaming(self._streaming)
        self._device_status.set_sps(self._current_sps)
        self._device_status.set_total_samples(self._data_count)

        # Also update status bar with SPS
        if self._streaming and self._current_sps > 0:
            self._statusbar.showMessage(
                f"🟢 Streaming @ {self._current_sps:.0f} SPS  |  "
                f"总样本: {self._data_count}"
            )

    def _refresh_channel_status(self):
        if self._streaming:
            status = self.processor.get_channel_status()
            self.signals.channel_status_changed.emit(status)

    # ── v2.4.0: EEG waveform channel mode helpers ───────────────────────

    def _apply_eeg_ch_mode(self, mode_idx: int):
        """Configure EEGWaveWidget for the selected channel group."""
        defs = self._eeg_ch_defs
        if mode_idx == 0:
            # Hand + Foot (MI channels only)
            subset = [(idx, name, role) for idx, name, role in defs
                      if role in ("hand", "foot", "aux_hand", "aux_foot")]
        elif mode_idx == 1:
            # Blink channels
            subset = [(idx, name, role) for idx, name, role in defs
                      if role == "blink"]
        else:
            # All active (non-disabled, non-focus)
            from signal_processor import DISABLED_CHANNELS
            subset = [(idx, name, role) for idx, name, role in defs
                      if idx not in DISABLED_CHANNELS and role != "disabled"]
        self._eeg_wave.configure(subset)

    @pyqtSlot(int)
    def _on_eeg_ch_mode_changed(self, idx: int):
        self._apply_eeg_ch_mode(idx)

    @pyqtSlot(int)
    def _on_eeg_time_changed(self, idx: int):
        secs_map = {0: 3, 1: 5, 2: 10}
        secs = secs_map.get(idx, 5)
        # v2.4.12: Base height 400 for 8-channel visibility
        base_h = 400
        self._eeg_wave.setMinimumHeight(base_h)
        # Update the painter's display window by storing it
        self._eeg_wave._display_seconds = secs


        import signal_processor as sp
        low = self._low_thresh_slider.value()
        high = self._high_thresh_slider.value()
        sp.BLINK_THRESHOLD_LOW = float(low)
        sp.BLINK_THRESHOLD_HIGH = float(high)

    @pyqtSlot(str)
    def _append_log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self._log_area.append(f"<span style='color:#484f58'>[{ts}]</span> {msg}")
        sb = self._log_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def keyPressEvent(self, event):
        """v2.4.10: Esc key exits BCI control when this window has focus."""
        if self._bci_active and event.key() == Qt.Key.Key_Escape:
            logger.info("Esc key pressed in window — stopping BCI control")
            self._append_log("⌨️  键盘 Esc 键 — 退出 BCI 控制")
            self._stop_bci()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self._stop_all()
        event.accept()


# ══════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("HybridMI-BCI GUI")
    app.setApplicationVersion(APP_VERSION)

    app.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
