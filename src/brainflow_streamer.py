"""
BrainFlow Streamer  v2.0
========================
Connects to OpenBCI Cyton+Daisy board via BrainFlow SDK.

BrainFlow — Copyright (c) 2019 Andrey Parfenov
Licensed under the MIT License.
https://github.com/brainflow-dev/brainflow

Supports Serial (USB) and Bluetooth (BLED112 dongle) connections.

v2.0 additions:
  - Bluetooth device scanning (system-level BT discovery)
  - Bluetooth connection via BLED112 virtual serial port
  - Scan results include device name, MAC address, port path
"""

import threading
import time
import logging
import subprocess
import re
import platform
import numpy as np
from typing import Optional, Callable

logger = logging.getLogger(__name__)

SYSTEM = platform.system()

try:
    from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds
    from brainflow.data_filter import DataFilter, FilterTypes, AggOperations
    BRAINFLOW_AVAILABLE = True
except ImportError:
    BRAINFLOW_AVAILABLE = False
    logger.warning("BrainFlow not installed — running in SIMULATION mode.")

CYTON_DAISY_BOARD_ID = 2       # BoardIds.CYTON_DAISY_BOARD = 2
SAMPLE_RATE = 250


class BrainFlowStreamer:
    """
    Manages BrainFlow board connection and data streaming.
    
    v2.0: Added Bluetooth scanning and connection support.
    """

    def __init__(self,
                 serial_port: str = "",
                 data_callback: Optional[Callable] = None,
                 status_callback: Optional[Callable] = None):
        self.serial_port = serial_port
        self.data_callback = data_callback
        self.status_callback = status_callback

        self._board = None
        self._streaming = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._sim_mode = not BRAINFLOW_AVAILABLE

        # Bluetooth state
        self._bt_devices: list[dict] = []
        self._bt_scanning = False
        self._bt_connected = False
        self._bt_device = None

        self._eeg_channels = None

    def _emit_status(self, status: str, detail: str = ""):
        if self.status_callback:
            self.status_callback(status, detail)
        logger.info(f"[BrainFlowStreamer] {status}: {detail}")

    # ── Port listing ────────────────────────────────────────────────────────

    def get_available_ports(self) -> list[str]:
        """Return list of serial port candidates (USB + Bluetooth serial)."""
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        return [p.device for p in ports]

    # ── v2.2.1: Auto device detection ───────────────────────────────────────

    def detect_openbci_device(self) -> Optional[dict]:
        """
        Scan serial ports for an OpenBCI Cyton/Daisy board (via dongle).

        The OpenBCI dongle (FTDI USB↔Serial) appears as a serial port
        with specific USB VID/PID or description patterns. When the
        board is powered on, it auto-connects to the dongle and
        becomes available on the host serial port.

        Returns a dict with port info if a board is detected, or None.
            {'port': '/dev/cu.usbserial-XXX', 'description': '...',
             'hwid': 'USB VID:PID=...', 'name': 'OpenBCI Cyton'}
        """
        try:
            import serial.tools.list_ports
            ports = serial.tools.list_ports.comports()

            # OpenBCI Cyton uses FTDI FT232R chip
            # Common patterns on macOS:
            #   - /dev/cu.usbserial-*  (FTDI driver)
            #   - /dev/cu.usbmodem*    (CDC-ACM, some configs)
            #   - /dev/cu.SLAB_USBtoUART (Silicon Labs, Cyton v3)
            openbci_vids = {
                0x0403,  # FTDI (Cyton dongle)
                0x10C4,  # Silicon Labs CP210x (Cyton v3)
                0x067B,  # Prolific
            }
            openbci_keywords = [
                'usbserial', 'usbmodem', 'SLAB_USBtoUART',
                'cyton', 'openbci', 'ftdi', 'ft232', 'cp210',
                'bled112',  # BT dongle
            ]

            for p in ports:
                # Check by VID
                if p.vid is not None:
                    if p.vid in openbci_vids:
                        return {
                            'port': p.device,
                            'description': p.description or '',
                            'hwid': p.hwid or '',
                            'name': self._guess_device_name(p),
                            'vid': p.vid,
                            'pid': p.pid,
                        }

                # Check by keyword in device name / description / hwid
                combined = f"{p.device} {p.description or ''} {p.hwid or ''}".lower()
                if any(kw in combined for kw in openbci_keywords):
                    return {
                        'port': p.device,
                        'description': p.description or '',
                        'hwid': p.hwid or '',
                        'name': self._guess_device_name(p),
                        'vid': p.vid,
                        'pid': p.pid,
                    }

        except Exception as e:
            logger.debug(f"Device detection scan: {e}")

        return None

    @staticmethod
    def _guess_device_name(p) -> str:
        """Guess a human-readable name from port info."""
        desc = (p.description or '').strip()
        if desc:
            # Common: "USB Serial", "FT232R USB UART", "CP2102 USB to UART Bridge"
            if 'openbci' in desc.lower():
                return 'OpenBCI Cyton'
            if 'cyton' in desc.lower():
                return 'OpenBCI Cyton'
            if 'bled112' in desc.lower():
                return 'OpenBCI (BLED112 蓝牙)'
            if any(x in desc.lower() for x in ('ft232', 'ftdi')):
                return f'OpenBCI Dongle ({desc[:24]})'
            if any(x in desc.lower() for x in ('cp210', 'slab', 'silicon')):
                return f'OpenBCI Cyton v3 ({desc[:24]})'
            return desc[:30]
        hwid = (p.hwid or '').upper()
        if 'BLED112' in hwid:
            return 'OpenBCI (BLED112 蓝牙)'
        if 'FT232' in hwid:
            return 'OpenBCI Dongle (FTDI)'
        name = p.device.split('/')[-1] if '/' in p.device else p.device
        return f'串口设备 ({name})'

    # ── v2.0: Bluetooth scanning ────────────────────────────────────────────

    def scan_bluetooth(self) -> list[dict]:
        """
        Scan for Bluetooth devices compatible with OpenBCI.
        
        Uses macOS system_profiler for BT device discovery.
        On Windows, would use different mechanism.
        
        Returns list of dicts:
          [{'name': 'OpenBCI Cyton', 'address': 'XX:XX:XX:XX:XX:XX',
            'port': '/dev/cu.BLED112-...', 'type': 'bluetooth'}, ...]
        """
        self._bt_devices = []
        self._bt_scanning = True

        if SYSTEM == 'Darwin':
            self._bt_devices = self._scan_bluetooth_macos()
        else:
            # Windows fallback: try serial port scanning with BT keywords
            self._bt_devices = self._scan_bluetooth_serial()

        self._bt_scanning = False
        logger.info(f"Bluetooth scan found {len(self._bt_devices)} device(s)")
        return self._bt_devices

    def _scan_bluetooth_macos(self) -> list[dict]:
        """Scan Bluetooth devices on macOS using system_profiler."""
        devices = []

        try:
            # Method 1: Use system_profiler to get connected/paired BT devices
            result = subprocess.run(
                ['system_profiler', 'SPBluetoothDataType', '-json'],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0 and result.stdout.strip():
                import json
                try:
                    bt_data = json.loads(result.stdout)
                    devices = self._parse_bt_json(bt_data)
                except json.JSONDecodeError:
                    # Fall back to text parsing
                    devices = self._parse_bt_text(result.stdout)
        except subprocess.TimeoutExpired:
            logger.warning("Bluetooth system_profiler timed out")
        except Exception as e:
            logger.warning(f"Bluetooth system_profiler failed: {e}")

        # Method 2: Also scan serial ports for BLED112-style devices
        bt_serial_devices = self._scan_bluetooth_serial()
        # Merge, avoiding duplicates by port
        known_ports = {d.get('port', '') for d in devices}
        for d in bt_serial_devices:
            if d.get('port', '') not in known_ports:
                devices.append(d)

        return devices

    def _parse_bt_json(self, bt_data: dict) -> list[dict]:
        """Parse macOS system_profiler Bluetooth JSON output."""
        devices = []
        try:
            # Navigate: SPBluetoothDataType → device_connected / device_not_connected
            bt_info = bt_data.get('SPBluetoothDataType', [])
            if not bt_info:
                return devices

            for item in bt_info:
                if isinstance(item, dict):
                    # Connected devices
                    for section in ['device_connected', 'device_not_connected']:
                        dev_list = item.get(section, [])
                        if isinstance(dev_list, list):
                            for dev in dev_list:
                                if isinstance(dev, dict):
                                    self._add_bt_device_if_relevant(devices, dev)
        except Exception as e:
            logger.debug(f"BT JSON parse detail: {e}")
        return devices

    def _parse_bt_text(self, text: str) -> list[dict]:
        """Parse macOS system_profiler Bluetooth plain text output."""
        devices = []
        current_device = {}
        device_pattern = re.compile(r'^\s{8}([^:]+):\s*(.*)$')

        for line in text.split('\n'):
            m = device_pattern.match(line)
            if m:
                key, value = m.group(1).strip(), m.group(2).strip()
                if key == 'Name' or key == '名称':
                    if current_device.get('name'):
                        self._add_bt_device_if_relevant(devices, current_device)
                        current_device = {}
                    current_device['name'] = value
                elif key == 'Address' or key == '地址':
                    current_device['address'] = value
                elif key == 'RSSI':
                    try:
                        current_device['rssi'] = int(value.split()[0])
                    except (ValueError, IndexError):
                        current_device['rssi'] = value

        if current_device.get('name'):
            self._add_bt_device_if_relevant(devices, current_device)

        return devices

    def _add_bt_device_if_relevant(self, devices: list, dev: dict):
        """Check if a Bluetooth device is OpenBCI-related and add it."""
        name = dev.get('name', '')
        address = dev.get('address', '')

        # Filter for OpenBCI, Cyton, BLED112, or general BLE serial adapters
        relevant_keywords = [
            'openbci', 'cyton', 'bled112', 'ble', 'bluetooth',
            'brain', 'eeg', '神经', '脑电', 'hc-05', 'hc-06',
            'dsd tech', 'serial', 'uart', 'spp'
        ]

        name_lower = name.lower()
        if any(kw in name_lower for kw in relevant_keywords):
            devices.append({
                'name': name,
                'address': address,
                'port': f"BT:{address}" if address else None,
                'type': 'bluetooth',
                'rssi': dev.get('rssi'),
                'source': 'system_profiler'
            })

    def _scan_bluetooth_serial(self) -> list[dict]:
        """Scan serial ports for BLED112 or Bluetooth-serial devices."""
        devices = []
        try:
            import serial.tools.list_ports
            ports = serial.tools.list_ports.comports()
            for p in ports:
                name = p.device
                desc = p.description or ''
                hwid = p.hwid or ''

                # BLED112 typically shows up with 'BLED112' in description or hwid
                combined = f"{name} {desc} {hwid}".lower()
                bt_keywords = ['bled112', 'ble', 'bluetooth', 'spp', 'hc-05',
                               'hc-06', 'dsd tech', 'uart bridge']

                if any(kw in combined for kw in bt_keywords):
                    devices.append({
                        'name': desc or name,
                        'address': hwid if 'BLED' in hwid.upper() else '',
                        'port': name,
                        'type': 'bluetooth_serial',
                        'description': desc,
                        'source': 'serial_scan'
                    })

                # Also include any cu.* device that's not a typical USB serial
                if SYSTEM == 'Darwin':
                    if ('cu.BLED' in name or 'cu.BT' in name or
                        'cu.DSD' in name or 'cu.HC' in name):
                        if not any(d['port'] == name for d in devices):
                            devices.append({
                                'name': f"Bluetooth设备 ({name})",
                                'address': '',
                                'port': name,
                                'type': 'bluetooth_serial',
                                'description': desc,
                                'source': 'serial_scan'
                            })
        except Exception as e:
            logger.warning(f"Serial BT scan failed: {e}")

        return devices

    # ── v2.0: Bluetooth connection ──────────────────────────────────────────

    def connect_bluetooth(self, device_info: dict) -> bool:
        """
        Connect to OpenBCI via Bluetooth (BLED112 dongle).
        
        device_info: dict from scan_bluetooth() result
          Must contain 'port' key (serial port path for BLED112).
        """
        if self._sim_mode:
            self._bt_device = device_info
            self._bt_connected = True
            self._emit_status("connected", "蓝牙模拟模式已连接")
            return True

        port = device_info.get('port', '')
        if not port:
            self._emit_status("error", "蓝牙设备缺少端口信息")
            return False

        # BLED112 appears as a serial port — use standard serial connection
        self.serial_port = port
        self._bt_device = device_info

        try:
            BoardShim.enable_dev_board_logger()
            params = BrainFlowInputParams()
            params.serial_port = port

            self._board = BoardShim(CYTON_DAISY_BOARD_ID, params)
            self._board.prepare_session()

            self._eeg_channels = BoardShim.get_eeg_channels(CYTON_DAISY_BOARD_ID)
            if len(self._eeg_channels) < 16:
                raise RuntimeError(
                    f"Expected 16 EEG channels, got {len(self._eeg_channels)}"
                )
            self._eeg_channels = self._eeg_channels[:16]

            self._bt_connected = True
            device_name = device_info.get('name', port)
            self._emit_status("connected",
                              f"蓝牙设备: {device_name} ({port})")
            return True

        except Exception as e:
            self._bt_connected = False
            self._emit_status("error", f"蓝牙连接失败: {e}")
            return False

    @property
    def bt_connected(self) -> bool:
        return self._bt_connected

    @property
    def bt_device_info(self) -> Optional[dict]:
        return self._bt_device

    # ── Serial connection (USB) ─────────────────────────────────────────────

    def connect(self, serial_port: str = "") -> bool:
        """Connect via serial/USB port."""
        if serial_port:
            self.serial_port = serial_port

        if self._sim_mode:
            self._emit_status("connected", "SIMULATION MODE")
            return True

        try:
            BoardShim.enable_dev_board_logger()
            params = BrainFlowInputParams()
            params.serial_port = self.serial_port

            self._board = BoardShim(CYTON_DAISY_BOARD_ID, params)
            self._board.prepare_session()

            self._eeg_channels = BoardShim.get_eeg_channels(CYTON_DAISY_BOARD_ID)
            if len(self._eeg_channels) < 16:
                raise RuntimeError(
                    f"Expected 16 EEG channels, got {len(self._eeg_channels)}"
                )
            self._eeg_channels = self._eeg_channels[:16]

            self._emit_status("connected", f"Port: {self.serial_port}")
            return True

        except Exception as e:
            self._emit_status("error", str(e))
            return False

    # ── Streaming ───────────────────────────────────────────────────────────

    def start_streaming(self) -> bool:
        if self._streaming:
            return True

        self._stop_event.clear()

        if not self._sim_mode and self._board is None:
            self._emit_status("error", "Not connected")
            return False

        try:
            if not self._sim_mode:
                self._board.start_stream(45000)

            self._streaming = True
            self._thread = threading.Thread(
                target=self._stream_loop,
                daemon=True,
                name="BrainFlowStream"
            )
            self._thread.start()
            self._emit_status("streaming", "Data acquisition started")
            return True

        except Exception as e:
            self._emit_status("error", f"Start stream failed: {e}")
            return False

    def stop_streaming(self):
        self._stop_event.set()
        self._streaming = False
        if self._thread:
            self._thread.join(timeout=3.0)
        if not self._sim_mode and self._board:
            try:
                self._board.stop_stream()
            except Exception:
                pass
        self._emit_status("stopped", "Streaming stopped")

    def disconnect(self):
        self.stop_streaming()
        if not self._sim_mode and self._board:
            try:
                self._board.release_session()
            except Exception:
                pass
            self._board = None
        self._bt_connected = False
        self._bt_device = None
        self._emit_status("disconnected", "Board released")

    # ── Stream loop ─────────────────────────────────────────────────────────

    def _stream_loop(self):
        POLL_INTERVAL = 0.05
        # --- error throttle: only escalate to "error" after N consecutive failures ---
        # Single transient BrainFlow exceptions (e.g. INVALID_ARGUMENTS_ERROR:13
        # "unable to obtain buffer size") happen when the board is momentarily busy;
        # they should NOT immediately reset the connection state and re-trigger the
        # device-detection dialog.  We require at least CONSEC_FAIL_THRESHOLD
        # consecutive failures before reporting a true board error.
        CONSEC_FAIL_THRESHOLD = 10   # ~0.5 s of continuous failure
        consec_fail = 0
        last_error_msg = ""

        while not self._stop_event.is_set():
            time.sleep(POLL_INTERVAL)
            try:
                if self._sim_mode:
                    data = self._simulate_data()
                else:
                    n = self._board.get_board_data_count()
                    if n < 1:
                        consec_fail = 0  # successful call, reset counter
                        continue
                    raw = self._board.get_board_data(n)
                    data = raw[self._eeg_channels, :]

                # Successful data read — reset failure counter
                consec_fail = 0
                last_error_msg = ""

                if self.data_callback and data.shape[1] > 0:
                    self.data_callback(data, time.time())

            except Exception as e:
                err_msg = str(e)
                consec_fail += 1
                # Log at DEBUG for transient errors to avoid flooding the UI log
                if consec_fail < CONSEC_FAIL_THRESHOLD:
                    logger.debug(f"Stream loop transient error ({consec_fail}/{CONSEC_FAIL_THRESHOLD}): {err_msg}")
                else:
                    # Only log at ERROR level and emit status once per distinct error
                    if err_msg != last_error_msg:
                        last_error_msg = err_msg
                        logger.error(f"Stream loop persistent error: {err_msg}")
                        if not self._stop_event.is_set():
                            self._emit_status("error", err_msg)

    # ── Simulation ──────────────────────────────────────────────────────────
    _sim_t = 0.0
    _sim_blink_next = 5.0
    _sim_mi_next = 8.0

    def _simulate_data(self) -> np.ndarray:
        """Generate 12 samples of synthetic 16-channel data at 250 Hz."""
        n = 12
        dt = n / SAMPLE_RATE
        t = np.linspace(self._sim_t, self._sim_t + dt, n, endpoint=False)
        self._sim_t += dt

        data = np.zeros((16, n))

        # Alpha background on all active EEG channels (including CP3/CP4 now)
        for ch in [2, 3, 4, 5, 6, 9, 10, 11, 12, 13]:
            data[ch] = 20.0 * np.sin(2 * np.pi * 10.0 * t)
            data[ch] += np.random.randn(n) * 5.0

        # Frontal theta
        for ch in [2, 9]:
            data[ch] += 8.0 * np.sin(2 * np.pi * 6.0 * t)

        # Simulate blink on CH1/CH2/CH9/CH10 (v2.3.1: all four blink channels)
        if self._sim_t > self._sim_blink_next:
            for ch in [0, 1, 8, 9]:
                data[ch] += 200.0 * np.exp(
                    -((t - self._sim_blink_next) ** 2) / 0.003
                )
            self._sim_blink_next += 5.0 + np.random.uniform(0, 2)

        return data
