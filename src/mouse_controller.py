"""
Mouse Controller  v2.2
======================
Cross-platform smooth mouse movement and click control.

macOS: CoreGraphics via ctypes (bypasses PyObjC code-signature issues)
Other: pynput fallback

v2.2 — Continuous 2D Vector Control:
  Receives (dx, dy) ∈ [-1,1]² from MI detector instead of discrete
  directional events. The control loop smoothly interpolates toward
  the target vector for fluid, natural cursor movement.

  - Diagonal movement: simultaneous hand+foot MI produces diagonal vectors
  - Speed = focus_score × vector_magnitude → attention controls velocity
  - Eased interpolation prevents jerky direction changes
  - Sub-pixel accumulator for smooth pixel-level rendering at 60 Hz
"""

import threading
import time
import logging
import platform
import math
import ctypes
from ctypes import c_double, c_void_p, c_int, Structure
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

SYSTEM = platform.system()   # 'Darwin' | 'Windows' | 'Linux'

# ── Platform backend ─────────────────────────────────────────────────────────

_NATIVE_AVAILABLE = False
_pynput_available = False

if SYSTEM == "Darwin":
    try:
        _cg = ctypes.CDLL(
            '/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics'
        )

        class _CGPoint(Structure):
            _fields_ = [('x', c_double), ('y', c_double)]

        # Event source (HIDSystemState = 1)
        _cg.CGEventSourceCreate.argtypes = [c_int]
        _cg.CGEventSourceCreate.restype  = c_void_p
        _HID_SOURCE = _cg.CGEventSourceCreate(1)

        # Position read (create a null event then ask where it is)
        _cg.CGEventCreate.argtypes       = [c_void_p]
        _cg.CGEventCreate.restype        = c_void_p
        _cg.CGEventGetLocation.argtypes  = [c_void_p]
        _cg.CGEventGetLocation.restype   = _CGPoint

        # Warp (actually moves the on-screen cursor)
        _cg.CGWarpMouseCursorPosition.argtypes = [_CGPoint]
        _cg.CGWarpMouseCursorPosition.restype  = c_int

        # CFRelease — needed to free CGEvent objects (prevent leak @ 60 Hz)
        _cf = ctypes.CDLL(
            '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation'
        )
        _cf.CFRelease.argtypes = [c_void_p]
        _cf.CFRelease.restype  = None

        # Click events
        _cg.CGEventCreateMouseEvent.argtypes = [c_void_p, c_int, _CGPoint, c_int]
        _cg.CGEventCreateMouseEvent.restype  = c_void_p
        _cg.CGEventPost.argtypes             = [c_int, c_void_p]
        _SESSION_TAP = 1  # kCGSessionEventTap

        _NATIVE_AVAILABLE = True
        logger.info("Mouse backend: CoreGraphics/ctypes @ 60 Hz")
    except Exception as exc:
        logger.warning(f"CoreGraphics backend init failed: {exc}")

if not _NATIVE_AVAILABLE:
    try:
        from pynput.mouse import Button, Controller as _PynputController
        _pynput_available = True
        logger.info("Mouse backend: pynput")
    except ImportError:
        logger.warning("pynput not available — mouse control disabled")

_MOUSE_OK = _NATIVE_AVAILABLE or _pynput_available


# ── Control loop constants ────────────────────────────────────────────────────

TARGET_HZ        = 60           # desired control loop rate
TICK_INTERVAL    = 1.0 / TARGET_HZ   # ~16.67 ms
SPIN_GUARD       = 0.0015       # sleep to within 1.5 ms, then spin

# Acceleration: full speed reached after ACCEL_RAMP_TICKS ticks
ACCEL_RAMP_TICKS = 24          # 60 Hz × 0.4 s = 24 ticks

# v2.2: Direction smoothing factor (per tick, at 60 Hz)
# 0.12 → reaches ~63% of target in ~8 ticks (130ms), ~95% in ~24 ticks (400ms)
DIRECTION_SMOOTHING = 0.12

# Sub-pixel accumulator cap (prevents runaway on long key-hold)
ACCUM_CAP        = 20.0

DOUBLE_CLICK_INTERVAL = 0.12

# ── v2.2: Cardinal + Diagonal direction map ──────────────────────────────────

DIRECTION_MAP = {
    # Cardinal (single-limb MI)
    "left":       (-1.0,  0.0),
    "right":      ( 1.0,  0.0),
    "up":         ( 0.0, -1.0),
    "down":       ( 0.0,  1.0),
    # Diagonal (hand + foot combined MI)
    "up-left":    (-1.0, -1.0),
    "up-right":   ( 1.0, -1.0),
    "down-left":  (-1.0,  1.0),
    "down-right": ( 1.0,  1.0),
}

DIRECTION_DESC = {
    "left":       "左手MI → 左移",
    "right":      "右手MI → 右移",
    "up":         "右脚MI → 上移",
    "down":       "左脚MI → 下移",
    "up-left":    "左手+右脚 → 左上",
    "up-right":   "右手+右脚 → 右上",
    "down-left":  "左手+左脚 → 左下",
    "down-right": "右手+左脚 → 右下",
}

DIRECTION_ICON = {
    "left":       "⬅",
    "right":      "➡",
    "up":         "⬆",
    "down":       "⬇",
    "up-left":    "↖",
    "up-right":   "↗",
    "down-left":  "↙",
    "down-right": "↘",
}


# ══════════════════════════════════════════════════════════════════════════════
# BCIMouseController
# ══════════════════════════════════════════════════════════════════════════════

class BCIMouseController:
    """
    v2.2: Receives continuous (dx, dy) vectors for smooth cursor control.

    The control loop runs at 60 Hz, smoothing direction changes via
    eased interpolation. Speed is determined by focus score and
    vector magnitude.

    Diagonal movement (hand+foot MI simultaneously) works naturally:
    the (dx, dy) vector has both components non-zero, and the cursor
    moves smoothly in the diagonal direction.
    """

    def __init__(self):
        self._active        = False
        self._tick_count    = 0

        # v2.2: Continuous 2D vector control
        self._target_dx     = 0.0    # target horizontal direction [-1, 1]
        self._target_dy     = 0.0    # target vertical direction   [-1, 1]
        self._current_dx    = 0.0    # smoothed horizontal (used for movement)
        self._current_dy    = 0.0    # smoothed vertical   (used for movement)

        # Sub-pixel accumulators
        self._accum_x       = 0.0
        self._accum_y       = 0.0

        # Absolute cursor position cache
        self._cur_x         = 0.0
        self._cur_y         = 0.0
        self._pos_valid     = False

        self._focus_score   = 0.5

        self._lock          = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event    = threading.Event()
        self._pending_click = None
        self._click_lock    = threading.Lock()

        self.on_status_change: Optional[callable] = None

        # pynput fallback
        self._pynput_ctrl = None
        if _pynput_available:
            self._pynput_ctrl = _PynputController()

    # ── Public API ────────────────────────────────────────────────────────

    def start(self):
        if self._active:
            return
        # Prime the position cache before starting the loop
        if _NATIVE_AVAILABLE:
            try:
                null_ev = _cg.CGEventCreate(None)
                pt = _cg.CGEventGetLocation(null_ev)
                with self._lock:
                    self._cur_x, self._cur_y = pt.x, pt.y
                    self._pos_valid = True
            except Exception as exc:
                logger.warning(f"Failed to prime cursor position via CoreGraphics: {exc}")
                raise RuntimeError(
                    f"无法读取鼠标位置。请授予辅助功能权限："
                    f"系统偏好设置 → 隐私与安全性 → 辅助功能 → 添加本应用。"
                )
        elif self._pynput_ctrl:
            try:
                pos = self._pynput_ctrl.position
                with self._lock:
                    self._cur_x, self._cur_y = float(pos[0]), float(pos[1])
                    self._pos_valid = True
            except Exception as exc:
                logger.warning(f"Failed to prime cursor position via pynput: {exc}")
                # Fallback: use (0, 0) and relative moves
                with self._lock:
                    self._cur_x, self._cur_y = 0.0, 0.0
                    self._pos_valid = False
        else:
            raise RuntimeError(
                "鼠标控制后端不可用。请确认已安装 pynput 或运行在受支持的平台上。"
            )

        self._active = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._control_loop,
            daemon=True,
            name="BCIMouseControl"
        )
        self._thread.start()
        logger.info("BCIMouseController started @ %d Hz (v2.2 continuous vector)", TARGET_HZ)
        self._notify_status("active")

    def stop(self):
        """Stop the control loop and release mouse.

        v2.4.9: Non-blocking — just sets the stop flags.  The daemon
        thread will exit on its own at the next loop iteration.
        No more join(timeout) that blocks the Qt main thread."""
        self._active = False
        self._stop_event.set()
        # Reset smoothed vectors on stop
        with self._lock:
            self._current_dx = 0.0
            self._current_dy = 0.0
            self._target_dx = 0.0
            self._target_dy = 0.0
            self._accum_x = 0.0
            self._accum_y = 0.0
        logger.info("BCIMouseController stop signal sent")
        self._notify_status("inactive")

    # ── v2.2: Continuous vector control ────────────────────────────────────

    def set_vector(self, dx: float, dy: float):
        """
        Set the target direction vector for continuous control.

        dx: float ∈ [-1, 1] — horizontal direction
            -1 = full left, +1 = full right, 0 = no horizontal
        dy: float ∈ [-1, 1] — vertical direction
            -1 = full up,   +1 = full down,  0 = no vertical

        The control loop smoothly interpolates toward this target.
        Call with (0, 0) to gradually stop.
        """
        mag = math.hypot(dx, dy)
        with self._lock:
            self._target_dx = max(-1.0, min(1.0, dx))
            self._target_dy = max(-1.0, min(1.0, dy))

            # Reset tick count when vector changes significantly
            if abs(dx) < 0.05 and abs(dy) < 0.05:
                self._tick_count = max(0, self._tick_count - 1)

    def set_direction(self, direction: Optional[str]):
        """
        Legacy discrete direction control. Maps direction string to
        the continuous vector system.

        Supports v2.2 diagonal directions.
        """
        if direction and direction in DIRECTION_MAP:
            dx, dy = DIRECTION_MAP[direction]
        else:
            dx, dy = 0.0, 0.0

        # Scale by ERD confidence (legacy directions are binary)
        magnitude = 0.8  # default strength for legacy mode
        self.set_vector(dx * magnitude, dy * magnitude)

    def set_focus(self, score: float):
        with self._lock:
            self._focus_score = max(0.0, min(1.0, float(score)))

    def on_blink(self, event: str):
        if not self._active:
            return
        with self._click_lock:
            self._pending_click = event

    def on_mi(self, direction: str):
        """
        v2.2: Legacy discrete MI callback.
        Uses continuous vector internally for smooth movement.
        No auto-clear timer — direction persists until next update.
        """
        if not self._active:
            return
        self.set_direction(direction)

    # ── High-precision control loop ───────────────────────────────────────

    def _control_loop(self):
        t_next = time.perf_counter()

        while not self._stop_event.is_set():
            # ── v2.4.9: Quick exit check at top of loop ───────────────────
            if not self._active:
                logger.info("BCIMouseController _active=False, exiting control loop")
                break

            # ── v2.4.9: Sleep-based wait (yields Python GIL) ──────────────
            # The old spin-wait burned CPU with `while ... pass`, which
            # starved PyQt6 event loop and pynput listener threads of the
            # GIL.  Now we sleep for most of the wait and only do a short
            # spin for the final fraction.  Mouse control at 60 Hz doesn't
            # need sub-µs precision — 1 ms is perfectly fine.
            remaining = t_next - time.perf_counter()
            if remaining > 0.003:            # > 3 ms → sleep most of it
                time.sleep(remaining * 0.85)  # sleep 85 %, spin the rest
            # Short final spin for precision + stop check
            while time.perf_counter() < t_next:
                if self._stop_event.is_set() or not self._active:
                    break
            t_next += TICK_INTERVAL

            # ── Click ──────────────────────────────────────────────────────
            with self._click_lock:
                click_event = self._pending_click
                self._pending_click = None
            if click_event == "single":
                self._do_click(1)
            elif click_event == "double":
                self._do_click(2)

            # ── v2.2: Smooth vector interpolation ──────────────────────────
            with self._lock:
                target_dx = self._target_dx
                target_dy = self._target_dy
                focus = self._focus_score

                # Eased interpolation toward target
                self._current_dx += (target_dx - self._current_dx) * DIRECTION_SMOOTHING
                self._current_dy += (target_dy - self._current_dy) * DIRECTION_SMOOTHING

                # Clamp to avoid overshoot near zero
                if abs(self._current_dx) < 0.001:
                    self._current_dx = 0.0
                if abs(self._current_dy) < 0.001:
                    self._current_dy = 0.0

                cur_dx = self._current_dx
                cur_dy = self._current_dy

            # ── Movement ───────────────────────────────────────────────────
            mag = math.hypot(cur_dx, cur_dy)

            if mag > 0.001:
                # Normalize to keep diagonal speed same as cardinal
                if mag > 1.0:
                    ndx = cur_dx / mag
                    ndy = cur_dy / mag
                else:
                    ndx = cur_dx
                    ndy = cur_dy

                speed = self._focus_to_speed(focus)
                ramp  = 0.3 + 0.7 * min(
                    (self._tick_count + 1) / max(ACCEL_RAMP_TICKS, 1), 1.0
                )

                # Per-tick pixel delta = direction × speed × ramp × magnitude
                # magnitude controls how "strong" the MI signal is
                used_mag = min(mag, 1.0)  # cap at full speed
                fx = ndx * speed * ramp * used_mag
                fy = ndy * speed * ramp * used_mag

                with self._lock:
                    self._tick_count = min(self._tick_count + 1, ACCEL_RAMP_TICKS)
                    self._accum_x = max(-ACCUM_CAP, min(ACCUM_CAP,
                                        self._accum_x + fx))
                    self._accum_y = max(-ACCUM_CAP, min(ACCUM_CAP,
                                        self._accum_y + fy))
                    ix = int(self._accum_x)
                    iy = int(self._accum_y)
                    self._accum_x -= ix
                    self._accum_y -= iy

                if ix != 0 or iy != 0:
                    # v2.4.9: Guard against warp during shutdown
                    if self._active and not self._stop_event.is_set():
                        self._warp(ix, iy)
            else:
                # No movement — drain accumulators and reset tick count
                with self._lock:
                    self._tick_count = 0
                    self._accum_x *= 0.5  # gentle decay
                    self._accum_y *= 0.5

    # ── Backend helpers ───────────────────────────────────────────────────

    def _focus_to_speed(self, score: float) -> float:
        """Map focus [0,1] → pixels/tick at 60 Hz."""
        from signal_processor import FOCUS_MIN_SPEED, FOCUS_MAX_SPEED
        base = FOCUS_MIN_SPEED + score * (FOCUS_MAX_SPEED - FOCUS_MIN_SPEED)
        # Scale from 20 Hz base to TARGET_HZ
        return base * (20.0 / TARGET_HZ)

    def _warp(self, dx: int, dy: int) -> None:
        """Move cursor by (dx, dy) using fastest available method.

        v2.4.8: Incremental mode — reads the *actual* system cursor position
        first, then adds the BCI offset. This allows the trackpad to work
        simultaneously with BCI control — both inputs stack on top of each
        other instead of BCI overwriting the trackpad.

        v2.4.9: CFRelease the null event after reading position (prevents
        CFObject leak at 60 Hz).
        """
        if _NATIVE_AVAILABLE:
            # Read REAL system cursor position (not our cached estimate)
            null_ev = _cg.CGEventCreate(None)
            pt = _cg.CGEventGetLocation(null_ev)
            _cf.CFRelease(null_ev)                    # v2.4.9: prevent leak
            nx = pt.x + dx
            ny = pt.y + dy
            _cg.CGWarpMouseCursorPosition(_CGPoint(nx, ny))
            # Update cache to match reality
            with self._lock:
                self._cur_x = nx
                self._cur_y = ny
        elif self._pynput_ctrl:
            self._pynput_ctrl.move(dx, dy)

    def _do_click(self, count: int = 1) -> None:
        """Post click event(s) at the current cursor position.

        v2.4.8: Read real system cursor position for clicks too.
        v2.4.9: CFRelease the null event after reading position.
        """
        if _NATIVE_AVAILABLE:
            null_ev = _cg.CGEventCreate(None)
            pt = _cg.CGEventGetLocation(null_ev)
            _cf.CFRelease(null_ev)                    # v2.4.9: prevent leak
            for _ in range(count):
                down = _cg.CGEventCreateMouseEvent(_HID_SOURCE, 1, pt, 0)
                up   = _cg.CGEventCreateMouseEvent(_HID_SOURCE, 2, pt, 0)
                _cg.CGEventPost(_SESSION_TAP, down)
                _cg.CFRelease(down)                   # v2.4.9: prevent leak
                time.sleep(0.01)
                _cg.CGEventPost(_SESSION_TAP, up)
                _cg.CFRelease(up)                     # v2.4.9: prevent leak
                if count > 1:
                    time.sleep(DOUBLE_CLICK_INTERVAL)
        elif self._pynput_ctrl:
            self._pynput_ctrl.click(Button.left, count)

    # ── Position read (used by self_test & external callers) ──────────────

    @property
    def _position(self) -> Tuple[float, float]:
        if _NATIVE_AVAILABLE:
            null_ev = _cg.CGEventCreate(None)
            pt = _cg.CGEventGetLocation(null_ev)
            _cf.CFRelease(null_ev)                    # v2.4.9: prevent leak
            with self._lock:
                self._cur_x, self._cur_y = pt.x, pt.y
            return (pt.x, pt.y)
        elif self._pynput_ctrl:
            return self._pynput_ctrl.position
        return (0.0, 0.0)

    # ══════════════════════════════════════════════════════════════════════
    #  Self-test: move to screen center, draw a screen-sized cross, return
    # ══════════════════════════════════════════════════════════════════════

    # One-time setup for screen-size query via CoreGraphics
    _SCREEN_QUERY_OK = False
    if _NATIVE_AVAILABLE:
        try:
            class __CGSize(Structure):
                _fields_ = [('width', c_double), ('height', c_double)]

            class __CGRect(Structure):
                _fields_ = [('origin', _CGPoint), ('size', __CGSize)]

            _cg.CGMainDisplayID.restype = ctypes.c_uint32
            _cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
            _cg.CGDisplayBounds.restype = __CGRect
            _SCREEN_QUERY_OK = True
        except Exception:
            pass

    def _get_screen_size(self) -> tuple:
        """Return (width, height) of the main display (cross-platform)."""
        if self._SCREEN_QUERY_OK:
            try:
                display_id = _cg.CGMainDisplayID()
                bounds = _cg.CGDisplayBounds(display_id)
                return (int(bounds.size.width), int(bounds.size.height))
            except Exception:
                pass
        elif SYSTEM == "Windows":
            try:
                user32 = ctypes.windll.user32
                return (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
            except Exception:
                pass
        return (1920, 1080)

    def _warp_abs(self, x: int, y: int):
        """Warp cursor to absolute screen position (cross-platform)."""
        if _NATIVE_AVAILABLE:
            _cg.CGWarpMouseCursorPosition(_CGPoint(float(x), float(y)))
            with self._lock:
                self._cur_x, self._cur_y = float(x), float(y)
        elif self._pynput_ctrl:
            self._pynput_ctrl.position = (x, y)
            with self._lock:
                self._cur_x, self._cur_y = float(x), float(y)

    def _draw_line(self, x1: int, y1: int, x2: int, y2: int, steps: int,
                   delay: float = 0.0):
        """Draw a straight line using N steps, with optional per-step delay."""
        dx = x2 - x1
        dy = y2 - y1
        steps = max(1, steps)
        for i in range(1, steps + 1):
            t = i / steps
            nx = int(x1 + dx * t)
            ny = int(y1 + dy * t)
            self._warp_abs(nx, ny)
            if delay > 0:
                time.sleep(delay)

    _SELF_TEST_DURATION = 3.0  # seconds — v2.2.5: human-visible pace

    def self_test(self, status_callback=None) -> bool:
        """
        Self-test: move cursor to center → draw screen-sized cross → return.

        Phases:
          1. Save origin → warp to screen center
          2. Horizontal bar: center→left edge→right edge→center
          3. Vertical bar:   center→top edge→bottom edge→center
          4. Return to original position

        Total duration ~3.0 s (human-visible pace).
        No EEG data needed — pure software self-check.
        """
        if not _MOUSE_OK:
            msg = "鼠标控制模块不可用，无法执行自检"
            logger.warning(msg)
            if status_callback:
                status_callback("error", {"message": msg})
            return False

        # 1. Save origin
        orig_x, orig_y = self._position
        logger.info("Self-test starting at (%.0f, %.0f)", orig_x, orig_y)

        # 2. Get screen & center
        screen_w, screen_h = self._get_screen_size()
        cx, cy = screen_w // 2, screen_h // 2

        # Total warp calls across all _draw_line phases:
        #   horiz: 120+200+120 = 440, vert: 100+200+100 = 400 → total 840
        total_warps = 440 + 400
        delay = max(self._SELF_TEST_DURATION / total_warps, 0.001)

        try:
            # ── Phase 1: move to center ──
            self._warp_abs(cx, cy)
            time.sleep(0.1)  # brief pause so user sees the jump
            if status_callback:
                status_callback("progress", {"phase": "center", "step": 0, "total_steps": 3})

            # ── Phase 2: horizontal cross bar ──
            self._draw_line(cx, cy, 0, cy, steps=120, delay=delay)
            self._draw_line(0, cy, screen_w - 1, cy, steps=200, delay=delay)
            self._draw_line(screen_w - 1, cy, cx, cy, steps=120, delay=delay)
            if status_callback:
                status_callback("progress", {"phase": "horizontal", "step": 1, "total_steps": 3})

            # ── Phase 3: vertical cross bar ──
            self._draw_line(cx, cy, cx, 0, steps=100, delay=delay)
            self._draw_line(cx, 0, cx, screen_h - 1, steps=200, delay=delay)
            self._draw_line(cx, screen_h - 1, cx, cy, steps=100, delay=delay)
            if status_callback:
                status_callback("progress", {"phase": "vertical", "step": 2, "total_steps": 3})

            # ── Phase 4: return to origin ──
            self._warp_abs(int(orig_x), int(orig_y))
            if status_callback:
                status_callback("progress", {"phase": "return", "step": 3, "total_steps": 3})

            result = {
                "success":  True,
                "origin":   (orig_x, orig_y),
                "center":   (cx, cy),
                "screen":   (screen_w, screen_h),
            }
            logger.info("Self-test done — back to (%.0f, %.0f)", orig_x, orig_y)
            if status_callback:
                status_callback("complete", result)
            return True

        except Exception as exc:
            logger.error("Self-test failed: %s", exc)
            if status_callback:
                status_callback("error", {"message": str(exc)})
            return False

    # ── Misc helpers ──────────────────────────────────────────────────────

    def _notify_status(self, status: str):
        if self.on_status_change:
            try:
                self.on_status_change(status)
            except Exception:
                pass

    @property
    def is_active(self) -> bool:
        return self._active
