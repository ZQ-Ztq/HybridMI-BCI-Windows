"""
BCI Signal Processor  v2.3.2
============================
Handles all EEG/EMG signal processing for the OpenBCI Cyton+Daisy 16-channel setup.

Data acquisition powered by BrainFlow SDK — Copyright (c) 2019 Andrey Parfenov (MIT)
https://github.com/brainflow-dev/brainflow

v2.2 Channel Layout — 8 Head-Top Channels for Hand+Foot MI:
  ┌─────────────────────────────────────────────────────────────┐
  │  HEAD-TOP 8 channels (motor cortex area)                    │
  │                                                             │
  │  LEFT hemisphere              RIGHT hemisphere              │
  │  ────────────────             ─────────────────             │
  │  CH4(idx3)  Aux-Hand L        CH12(idx11) Aux-Hand R       │
  │  CH5(idx4)  HAND-L  ★primary  CH13(idx12) HAND-R  ★primary │
  │  CH6(idx5)  FOOT-L  ★primary  CH14(idx13) FOOT-R  ★primary │
  │  CH7(idx6)  Aux-Foot L        CH15(idx14) Aux-Foot R       │
  │                                                             │
  │  OTHER channels:                                            │
  │    CH1(idx0)  — Blink (frontal EMG)       (v2.3.1)         │
  │    CH2(idx1)  — Blink (frontal EMG)       (v2.3.1)         │
  │    CH3(idx2)  — Focus (frontal EEG)                         │
  │    CH8(idx7)  — DISABLED                                    │
  │    CH9(idx8)  — Blink (frontal EMG)                         │
  │    CH10(idx9) — Blink (frontal EMG)                         │
  │    CH11(idx10)— Focus (frontal EEG)                        │
  │    CH16(idx15)— DISABLED                                    │
  └─────────────────────────────────────────────────────────────┘

v2.2 Motor Imagery Mapping (contralateral ERD):
  ┌─────────────┬───────────────┬────────────┬──────────────┐
  │  Imagination │ ERD Side      │ Hemisphere │ Direction    │
  ├─────────────┼───────────────┼────────────┼──────────────┤
  │  Left hand   │ Right hemi    │ L power > R│ LEFT  (←)    │
  │  Right hand  │ Left  hemi    │ R power > L│ RIGHT (→)    │
  │  Left foot   │ Right hemi    │ L power > R│ DOWN  (↓)    │
  │  Right foot  │ Left  hemi    │ R power > L│ UP    (↑)    │
  └─────────────┴───────────────┴────────────┴──────────────┘

v2.2 Diagonal Directions (hand + foot simultaneously):
  ┌────────────┬──────────────────────┬───────────┐
  │  Direction │ Combination          │ (dx, dy)  │
  ├────────────┼──────────────────────┼───────────┤
  │  左上 ↖     │ Left hand + Right ft │ (-1, -1)  │
  │  左下 ↙     │ Left hand + Left ft  │ (-1, +1)  │
  │  右上 ↗     │ Right hand + Right ft│ (+1, -1)  │
  │  右下 ↘     │ Right hand + Left ft │ (+1, +1)  │
  └────────────┴──────────────────────┴───────────┘

v2.2 Continuous Scoring:
  Instead of discrete directional events, the MI detector produces a
  continuous 2D vector (dx, dy) ∈ [-1,1]². The mouse controller uses
  this vector for smooth, fluid movement with easing interpolation.
"""

import numpy as np
from scipy import signal as sp_signal
from collections import deque
import threading
import time
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════
# v2.2 Channel Layout (0-indexed)
# ══════════════════════════════════════════════════════════════════════════

# ── Hand MI channels (left/right hemisphere) ──
HAND_L_IDX      = 4    # CH5  — left  hemisphere, hand motor area (C3)
HAND_R_IDX      = 12   # CH13 — right hemisphere, hand motor area (C4)
AUX_HAND_L_IDX  = 3    # CH4  — left  hemisphere, hand auxiliary
AUX_HAND_R_IDX  = 11   # CH12 — right hemisphere, hand auxiliary

# ── Foot MI channels (left/right hemisphere) ──
FOOT_L_IDX      = 5    # CH6  — left  hemisphere, foot motor area
FOOT_R_IDX      = 13   # CH14 — right hemisphere, foot motor area
AUX_FOOT_L_IDX  = 6    # CH7  — left  hemisphere, foot auxiliary
AUX_FOOT_R_IDX  = 14   # CH15 — right hemisphere, foot auxiliary

# ── All 8 head-top channels (used for MI detection) ──
HEAD_TOP_CHANNELS = [
    AUX_HAND_L_IDX, HAND_L_IDX, FOOT_L_IDX, AUX_FOOT_L_IDX,    # left hemisphere
    AUX_HAND_R_IDX, HAND_R_IDX, FOOT_R_IDX, AUX_FOOT_R_IDX,    # right hemisphere
]

# ── Hand MI primary pair ──
HAND_PRIMARY = (HAND_L_IDX, HAND_R_IDX)      # (L, R)
HAND_AUX     = (AUX_HAND_L_IDX, AUX_HAND_R_IDX)

# ── Foot MI primary pair ──
FOOT_PRIMARY = (FOOT_L_IDX, FOOT_R_IDX)      # (L, R)
FOOT_AUX     = (AUX_FOOT_L_IDX, AUX_FOOT_R_IDX)

# ── Blink: CH1, CH2, CH9, CH10 (frontal EMG) ──  v2.3.1: all four blink
BLINK_CHANNELS = [0, 1, 8, 9]       # CH1, CH2, CH9, CH10 — blink EMG

# ── Focus: frontal EEG ──
FOCUS_CHANNELS = [2, 10]      # CH3, CH11

# ── Disabled channels ──
DISABLED_CHANNELS = [7, 15]   # CH8, CH16

# ── Other non-MI channels (still monitored for signal) ──
OTHER_CHANNELS = [2, 10]  # CH3, CH11 — v2.3.1: CH1/2 moved to blink

SAMPLE_RATE        = 250              # OpenBCI Cyton+Daisy sample rate — v2.2.3: 250Hz
BUFFER_SECONDS     = 2.0              # rolling window for analysis
BUFFER_SIZE        = int(SAMPLE_RATE * BUFFER_SECONDS)

# ── EMG/Blink parameters ──────────────────────────────────────────────────
EMG_HIGHPASS_HZ       = 20.0
EMG_LOWPASS_HZ        = 45.0
BLINK_THRESHOLD_LOW   = 80.0          # µV RMS - ordinary blink
BLINK_THRESHOLD_HIGH  = 150.0         # µV RMS - intentional blink
BLINK_MIN_DURATION_MS = 80
BLINK_MAX_DURATION_MS = 400
DOUBLE_BLINK_GAP_MS   = 800
BLINK_REFRACTORY_MS   = 600

# ── EEG / band parameters ─────────────────────────────────────────────────
ALPHA_LOW_HZ     = 8.0
ALPHA_HIGH_HZ    = 13.0
MU_LOW_HZ        = 8.0
MU_HIGH_HZ       = 12.0
BETA_LOW_HZ      = 13.0
BETA_HIGH_HZ     = 30.0

MI_WINDOW_SEC    = 1.5
MI_STEP_SEC      = 0.1             # v2.2: faster MI polling (100ms) for smoother control
MI_BUFFER_SIZE   = int(SAMPLE_RATE * MI_WINDOW_SEC)

# v2.2: ERD detection parameters
ERD_RATIO_THRESH = 0.10            # per-channel-pair asymmetry threshold
ERD_CONFIRM_COUNT = 2              # v2.2: reduced from 3 for faster response
AUX_AGREEMENT_WEIGHT = 0.3         # auxiliary channel weight in score

# ── Focus / attention parameters ──────────────────────────────────────────
FOCUS_WINDOW_SEC  = 3.0
FOCUS_BUFFER_SIZE = int(SAMPLE_RATE * FOCUS_WINDOW_SEC)
FOCUS_ALPHA_LOW   = 8.0
FOCUS_ALPHA_HIGH  = 12.0
FOCUS_THETA_LOW   = 4.0
FOCUS_THETA_HIGH  = 8.0
FOCUS_MIN_SPEED   = 2.0
FOCUS_MAX_SPEED   = 25.0
FOCUS_SMOOTH_ALPHA = 0.1


# ══════════════════════════════════════════════════════════════════════════
# Ring Buffer
# ══════════════════════════════════════════════════════════════════════════

class RingBuffer:
    """Thread-safe ring buffer for streaming EEG data."""

    def __init__(self, n_channels: int, size: int):
        self.n_channels = n_channels
        self.size = size
        self._buf = np.zeros((n_channels, size), dtype=np.float64)
        self._lock = threading.Lock()
        self._filled = 0

    def push(self, samples: np.ndarray):
        with self._lock:
            n = samples.shape[1]
            if n >= self.size:
                self._buf[:] = samples[:, -self.size:]
                self._filled = self.size
            else:
                self._buf[:, :-n] = self._buf[:, n:]
                self._buf[:, -n:] = samples
                self._filled = min(self._filled + n, self.size)

    def get(self) -> Tuple[np.ndarray, int]:
        with self._lock:
            return self._buf.copy(), self._filled


# ══════════════════════════════════════════════════════════════════════════
# DSP helpers
# ══════════════════════════════════════════════════════════════════════════

def bandpass_filter(data: np.ndarray, lowcut: float, highcut: float,
                    fs: float = SAMPLE_RATE, order: int = 4) -> np.ndarray:
    """Zero-phase Butterworth bandpass filter. data shape: (..., n_samples)"""
    nyq = 0.5 * fs
    low = lowcut / nyq
    high = highcut / nyq
    low = np.clip(low, 1e-4, 0.9999)
    high = np.clip(high, 1e-4, 0.9999)
    if low >= high:
        return data
    b, a = sp_signal.butter(order, [low, high], btype='band')
    return sp_signal.filtfilt(b, a, data, axis=-1)


def notch_filter(data: np.ndarray, freq: float = 50.0,
                 fs: float = SAMPLE_RATE, Q: float = 30.0) -> np.ndarray:
    """Notch filter for power-line interference (50 Hz EU / 60 Hz US)."""
    b, a = sp_signal.iirnotch(freq / (0.5 * fs), Q)
    return sp_signal.filtfilt(b, a, data, axis=-1)


def compute_band_power(data: np.ndarray, low: float, high: float,
                       fs: float = SAMPLE_RATE) -> float:
    """Compute mean band power via Welch PSD."""
    if data.ndim == 1:
        data = data[np.newaxis, :]
    freqs, psd = sp_signal.welch(data, fs=fs, nperseg=min(256, data.shape[-1]),
                                  axis=-1)
    idx = np.logical_and(freqs >= low, freqs <= high)
    if not idx.any():
        return 0.0
    return float(np.mean(psd[:, idx]))


# ══════════════════════════════════════════════════════════════════════════
# BlinkDetector (unchanged logic, kept for compatibility)
# ══════════════════════════════════════════════════════════════════════════

class BlinkDetector:
    """
    Detects intentional vs ordinary blinks from orbicularis oculi EMG.
    Uses dual-threshold: low = involuntary, high = intentional.
    Tracks blink timing for single / double blink classification.
    """

    def __init__(self, sample_rate: float = SAMPLE_RATE):
        self.fs = sample_rate
        self._state = "idle"
        self._blink_start = 0.0
        self._last_blink_end = 0.0
        self._last_blink_intentional = False
        self._pending_single = False
        self._pending_single_time = 0.0
        self._refractory_until = 0.0
        self.callbacks = []

        self._emg_buf = deque(maxlen=int(sample_rate * 0.05))

    def register_callback(self, fn):
        self.callbacks.append(fn)

    def _emit(self, event: str):
        for cb in self.callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.error(f"BlinkDetector callback error: {e}")

    def process(self, blink_channels_data: np.ndarray, timestamp: float):
        if timestamp < self._refractory_until:
            return

        n_new = blink_channels_data.shape[1]
        rms_vals = np.sqrt(np.mean(blink_channels_data ** 2, axis=0))
        for v in rms_vals:
            self._emg_buf.append(v)

        current_rms = float(np.mean(self._emg_buf)) if self._emg_buf else 0.0

        if self._state == "idle":
            if current_rms > BLINK_THRESHOLD_LOW:
                self._state = "in_blink"
                self._blink_start = timestamp
                self._blink_intentional = current_rms > BLINK_THRESHOLD_HIGH
        elif self._state == "in_blink":
            if current_rms > BLINK_THRESHOLD_HIGH:
                self._blink_intentional = True

            duration_ms = (timestamp - self._blink_start) * 1000.0

            if current_rms < BLINK_THRESHOLD_LOW * 0.6:
                if BLINK_MIN_DURATION_MS <= duration_ms <= BLINK_MAX_DURATION_MS:
                    self._on_blink_ended(timestamp)
                self._state = "idle"

            elif duration_ms > BLINK_MAX_DURATION_MS * 1.5:
                self._state = "idle"

        if self._pending_single:
            gap_ms = (timestamp - self._pending_single_time) * 1000.0
            if gap_ms > DOUBLE_BLINK_GAP_MS:
                self._pending_single = False
                if self._last_blink_intentional:
                    self._emit("single")
                    self._refractory_until = timestamp + BLINK_REFRACTORY_MS / 1000.0

    def _on_blink_ended(self, t: float):
        gap_since_last = (t - self._last_blink_end) * 1000.0
        is_intentional = self._blink_intentional

        if self._pending_single and gap_since_last < DOUBLE_BLINK_GAP_MS:
            self._pending_single = False
            if self._last_blink_intentional or is_intentional:
                self._emit("double")
                self._refractory_until = t + BLINK_REFRACTORY_MS / 1000.0
        else:
            self._pending_single = True
            self._pending_single_time = t

        self._last_blink_end = t
        self._last_blink_intentional = is_intentional


# ══════════════════════════════════════════════════════════════════════════
# v2.2 MotorImageryDetector — continuous 2D vector output
# ══════════════════════════════════════════════════════════════════════════

class MotorImageryDetector:
    """
    v2.2: Hand+Foot MI detector with continuous 2D vector output.

    Hand MI (left ↔ right): compares L vs R hand motor channels.
    Foot MI (up ↔ down):    compares L vs R foot motor channels.

    Auxiliary channels provide confirmation — if aux disagrees with primary,
    the score is dampened.

    The detector produces a (dx, dy) vector ∈ [-1,1]² where:
      dx = -hand_score   (hand MI → horizontal)
      dy = +foot_score   (foot MI → vertical)

    Diagonal movement when both hand and foot MI are detected simultaneously.

    Consecutive-window confirmation prevents false triggers.
    """

    def __init__(self, sample_rate: float = SAMPLE_RATE):
        self.fs = sample_rate
        self._hand_confirm = deque(maxlen=ERD_CONFIRM_COUNT)
        self._foot_confirm = deque(maxlen=ERD_CONFIRM_COUNT)

        # Smoothed output (EMA) for fluid cursor control
        self._dx_smooth = 0.0
        self._dy_smooth = 0.0
        self._ema_alpha = 0.3  # smoothing factor for direction changes

        self.callbacks = []

    def register_callback(self, fn):
        self.callbacks.append(fn)

    def _emit(self, direction: str):
        for cb in self.callbacks:
            try:
                cb(direction)
            except Exception as e:
                logger.error(f"MI callback error: {e}")

    def _laterality_score(self, left_data: np.ndarray,
                          right_data: np.ndarray,
                          band_low: float, band_high: float) -> float:
        """
        Returns asymmetry: (L_power - R_power) / (L_power + R_power)
        Positive = L dominant → RIGHT hemisphere ERD → imagining LEFT side
        Negative = R dominant → LEFT  hemisphere ERD → imagining RIGHT side
        """
        p_l = compute_band_power(left_data, band_low, band_high, self.fs)
        p_r = compute_band_power(right_data, band_low, band_high, self.fs)
        total = p_l + p_r
        if total < 1e-12:
            return 0.0
        return (p_l - p_r) / total

    def _paired_score(self, primary_l: np.ndarray, primary_r: np.ndarray,
                      aux_l: np.ndarray, aux_r: np.ndarray) -> float:
        """
        Compute MI score for one axis (hand or foot).

        Primary pair + auxiliary pair are combined:
        - Primary weight: 1.0 - AUX_AGREEMENT_WEIGHT
        - Aux weight:      AUX_AGREEMENT_WEIGHT

        If aux direction disagrees with primary, it damps the total score.
        """
        # Primary pair — mu band (6:4 mu:beta ratio)
        score_pri_mu   = self._laterality_score(primary_l, primary_r, MU_LOW_HZ, MU_HIGH_HZ)
        score_pri_beta = self._laterality_score(primary_l, primary_r, BETA_LOW_HZ, 25.0)
        score_pri = 0.6 * score_pri_mu + 0.4 * score_pri_beta

        # Auxiliary pair
        score_aux_mu   = self._laterality_score(aux_l, aux_r, MU_LOW_HZ, MU_HIGH_HZ)
        score_aux_beta = self._laterality_score(aux_l, aux_r, BETA_LOW_HZ, 25.0)
        score_aux = 0.6 * score_aux_mu + 0.4 * score_aux_beta

        # Combine: if aux agrees with primary → full weight; if disagrees → dampened
        pri_weight = 1.0 - AUX_AGREEMENT_WEIGHT
        aux_weight = AUX_AGREEMENT_WEIGHT

        if (score_pri * score_aux) < 0:
            # Aux disagrees → reduce aux weight
            aux_weight *= 0.5

        return pri_weight * score_pri + aux_weight * score_aux

    def process_frame(self, mu_filtered: np.ndarray) -> Tuple[float, float, Optional[str]]:
        """
        Process one MI analysis frame.

        mu_filtered: (16, n_samples) mu-band filtered data.

        Returns:
            dx: float ∈ [-1, 1] — horizontal direction (negative=left, positive=right)
            dy: float ∈ [-1, 1] — vertical direction   (negative=up,    positive=down)
            label: Optional[str] — human-readable direction label
        """
        min_len = 32
        if mu_filtered.shape[1] < min_len:
            return (0.0, 0.0, None)

        # ── Hand MI ────────────────────────────────────────────────────────
        hand_l_pri = mu_filtered[HAND_PRIMARY[0], :]
        hand_r_pri = mu_filtered[HAND_PRIMARY[1], :]
        hand_l_aux = mu_filtered[HAND_AUX[0], :]
        hand_r_aux = mu_filtered[HAND_AUX[1], :]

        l_rms = float(np.sqrt(np.mean(hand_l_pri ** 2)))
        r_rms = float(np.sqrt(np.mean(hand_r_pri ** 2)))

        hand_score = 0.0
        if l_rms > 0.5 or r_rms > 0.5:
            hand_score = self._paired_score(
                hand_l_pri, hand_r_pri, hand_l_aux, hand_r_aux
            )

        # ── Foot MI ────────────────────────────────────────────────────────
        foot_l_pri = mu_filtered[FOOT_PRIMARY[0], :]
        foot_r_pri = mu_filtered[FOOT_PRIMARY[1], :]
        foot_l_aux = mu_filtered[FOOT_AUX[0], :]
        foot_r_aux = mu_filtered[FOOT_AUX[1], :]

        fl_rms = float(np.sqrt(np.mean(foot_l_pri ** 2)))
        fr_rms = float(np.sqrt(np.mean(foot_r_pri ** 2)))

        foot_score = 0.0
        if fl_rms > 0.5 or fr_rms > 0.5:
            foot_score = self._paired_score(
                foot_l_pri, foot_r_pri, foot_l_aux, foot_r_aux
            )

        # ── Thresholding ───────────────────────────────────────────────────
        hand_signed = hand_score if abs(hand_score) > ERD_RATIO_THRESH else 0.0
        foot_signed = foot_score if abs(foot_score) > ERD_RATIO_THRESH else 0.0

        # ── Confirmation window ────────────────────────────────────────────
        hand_dir = None
        if hand_signed > 0:
            hand_dir = "left"
        elif hand_signed < 0:
            hand_dir = "right"
        self._hand_confirm.append(hand_dir)

        foot_dir = None
        if foot_signed > 0:
            foot_dir = "down"
        elif foot_signed < 0:
            foot_dir = "up"
        self._foot_confirm.append(foot_dir)

        # Apply confirmation: only if consistent across window
        hand_confirmed = (
            len(self._hand_confirm) == ERD_CONFIRM_COUNT and
            all(d == hand_dir for d in self._hand_confirm) and
            hand_dir is not None
        )
        foot_confirmed = (
            len(self._foot_confirm) == ERD_CONFIRM_COUNT and
            all(d == foot_dir for d in self._foot_confirm) and
            foot_dir is not None
        )

        # ── Produce 2D vector ─────────────────────────────────────────────
        # dx = -hand_score (left hand → right ERD → L>R → hand_score>0 → dx=-1 ← left)
        # dy = +foot_score (left foot → right ERD → L>R → foot_score>0 → dy=+1 ↓ down)
        raw_dx = -hand_signed if hand_confirmed else 0.0
        raw_dy = +foot_signed if foot_confirmed else 0.0

        # Clamp to [-1, 1]
        raw_dx = max(-1.0, min(1.0, raw_dx))
        raw_dy = max(-1.0, min(1.0, raw_dy))

        # ── EMA smoothing for fluid transitions ────────────────────────────
        self._dx_smooth = self._ema_alpha * raw_dx + (1 - self._ema_alpha) * self._dx_smooth
        self._dy_smooth = self._ema_alpha * raw_dy + (1 - self._ema_alpha) * self._dy_smooth

        # ── Direction label ────────────────────────────────────────────────
        label = None
        dx_s = self._dx_smooth
        dy_s = self._dy_smooth

        if abs(dx_s) < 0.05 and abs(dy_s) < 0.05:
            label = None
        elif abs(dx_s) < 0.08:
            # Pure vertical
            if dy_s > 0.05:
                label = "down"
            elif dy_s < -0.05:
                label = "up"
        elif abs(dy_s) < 0.08:
            # Pure horizontal
            if dx_s < -0.05:
                label = "left"
            elif dx_s > 0.05:
                label = "right"
        else:
            # Diagonal
            if dx_s < -0.05 and dy_s < -0.05:
                label = "up-left"
            elif dx_s < -0.05 and dy_s > 0.05:
                label = "down-left"
            elif dx_s > 0.05 and dy_s < -0.05:
                label = "up-right"
            elif dx_s > 0.05 and dy_s > 0.05:
                label = "down-right"

        return (self._dx_smooth, self._dy_smooth, label)

    def reset_smoothing(self):
        """Reset EMA state (called when MI stops)."""
        self._dx_smooth = 0.0
        self._dy_smooth = 0.0


# ══════════════════════════════════════════════════════════════════════════
# FocusDetector
# ══════════════════════════════════════════════════════════════════════════

class FocusDetector:
    """
    Computes attention/focus index from frontal EEG.
    Uses beta/(alpha+theta) ratio → higher = more focused.
    Returns normalized [0,1] focus score smoothed via EMA.
    """

    def __init__(self, sample_rate: float = SAMPLE_RATE):
        self.fs = sample_rate
        self._score = 0.5
        self._calibrated = False
        self._cal_scores = []
        self._cal_mean = 1.0
        self._cal_std = 0.3

    def process(self, fp1_data: np.ndarray, fp2_data: np.ndarray) -> float:
        if len(fp1_data) < FOCUS_BUFFER_SIZE // 2:
            return self._score

        combined = np.mean([fp1_data, fp2_data], axis=0)

        alpha_p = compute_band_power(combined, FOCUS_ALPHA_LOW, FOCUS_ALPHA_HIGH, self.fs)
        theta_p = compute_band_power(combined, FOCUS_THETA_LOW, FOCUS_THETA_HIGH, self.fs)
        beta_p  = compute_band_power(combined, 14.0, 30.0, self.fs)

        denom = alpha_p + theta_p
        if denom < 1e-12:
            raw_score = 0.5
        else:
            raw_score = beta_p / denom

        if not self._calibrated:
            self._cal_scores.append(raw_score)
            if len(self._cal_scores) > 20:
                self._cal_mean = float(np.mean(self._cal_scores))
                self._cal_std  = max(float(np.std(self._cal_scores)), 0.01)
                self._calibrated = True
            normalized = 0.5
        else:
            z = (raw_score - self._cal_mean) / self._cal_std
            normalized = float(np.clip(0.5 + z * 0.15, 0.0, 1.0))

        self._score = FOCUS_SMOOTH_ALPHA * normalized + (1 - FOCUS_SMOOTH_ALPHA) * self._score
        return self._score

    def score_to_speed(self, score: float) -> float:
        return FOCUS_MIN_SPEED + score * (FOCUS_MAX_SPEED - FOCUS_MIN_SPEED)


# ══════════════════════════════════════════════════════════════════════════
# v2.2 SignalProcessor — top-level orchestrator
# ══════════════════════════════════════════════════════════════════════════

class SignalProcessor:
    """
    v2.2: Top-level processor — ties together all detectors.

    Uses 8 head-top channels for hand+foot MI detection with auxiliary
    verification. Produces a continuous 2D direction vector for smooth
    mouse cursor control.

    Hand MI controls LEFT/RIGHT, Foot MI controls UP/DOWN.
    Simultaneous hand+foot MI produces diagonal movement.
    """

    def __init__(self, sample_rate: float = SAMPLE_RATE):
        self.fs = sample_rate
        self.n_channels = 16
        self._ring = RingBuffer(self.n_channels, BUFFER_SIZE)
        self._mi_ring = RingBuffer(self.n_channels, MI_BUFFER_SIZE)
        self._focus_ring = RingBuffer(self.n_channels, FOCUS_BUFFER_SIZE)

        self.blink_detector = BlinkDetector(sample_rate)
        self.mi_detector = MotorImageryDetector(sample_rate)
        self.focus_detector = FocusDetector(sample_rate)

        self._last_mi_check = 0.0
        self._channel_activity = np.zeros(16, dtype=bool)
        self.all_channels_active = False

        self._active_channels = sorted(
            set(HEAD_TOP_CHANNELS + OTHER_CHANNELS + BLINK_CHANNELS)
        )  # all channels except DISABLED — v2.3: include blink CH9/CH10

        # Public callbacks
        self.on_blink   = None   # fn(event: 'single'|'double')
        self.on_mi      = None   # fn(direction: str) — legacy discrete callback
        self.on_vector  = None   # fn(dx: float, dy: float, label: str|None) — v2.2 continuous
        self.on_focus   = None   # fn(score: float)
        self.on_ready   = None   # fn() - all active channels have signal

        # Wire internal callbacks
        self.blink_detector.register_callback(self._blink_cb)

    def _blink_cb(self, event):
        if self.on_blink:
            self.on_blink(event)

    def push_data(self, raw: np.ndarray, timestamp: float):
        """raw: shape (16, n_samples) — raw µV data from BrainFlow"""
        # Zero out disabled channels
        for ch in DISABLED_CHANNELS:
            raw[ch, :] = 0.0

        # Notch filter
        filtered = notch_filter(raw.copy(), freq=50.0, fs=self.fs)

        # Check valid signal on each active channel
        for ch in self._active_channels:
            rms = float(np.sqrt(np.mean(filtered[ch] ** 2)))
            if rms > 0.5:
                self._channel_activity[ch] = True

        was_ready = self.all_channels_active
        self.all_channels_active = all(self._channel_activity[c] for c in self._active_channels)
        if self.all_channels_active and not was_ready and self.on_ready:
            self.on_ready()

        # Push to ring buffers
        self._ring.push(filtered)
        self._mi_ring.push(filtered)
        self._focus_ring.push(filtered)

        # Blink detection (EMG bandpass on frontal channels)
        emg_filtered = bandpass_filter(
            filtered[BLINK_CHANNELS, :], EMG_HIGHPASS_HZ, EMG_LOWPASS_HZ, self.fs
        )
        self.blink_detector.process(emg_filtered, timestamp)

        # MI detection (every MI_STEP_SEC — 100ms for smooth updates)
        now = timestamp
        if now - self._last_mi_check >= MI_STEP_SEC:
            self._last_mi_check = now
            mi_buf, filled = self._mi_ring.get()
            if filled >= MI_BUFFER_SIZE // 2:
                self._run_mi(mi_buf)

        # Focus detection
        focus_buf, focus_filled = self._focus_ring.get()
        if focus_filled >= FOCUS_BUFFER_SIZE // 2:
            fp1 = bandpass_filter(focus_buf[FOCUS_CHANNELS[0]:FOCUS_CHANNELS[0]+1, :],
                                   1.0, 40.0, self.fs)[0]
            fp2 = bandpass_filter(focus_buf[FOCUS_CHANNELS[1]:FOCUS_CHANNELS[1]+1, :],
                                   1.0, 40.0, self.fs)[0]
            score = self.focus_detector.process(fp1, fp2)
            if self.on_focus:
                self.on_focus(score)

    def _run_mi(self, buf: np.ndarray):
        """
        v2.2: Process hand + foot MI simultaneously.
        Produces continuous 2D direction vector with auxiliary verification.
        """
        # Band-pass for mu/beta rhythm (8-13 Hz captures mu rhythm)
        mu_filtered = bandpass_filter(buf, MU_LOW_HZ, ALPHA_HIGH_HZ, self.fs)

        dx, dy, label = self.mi_detector.process_frame(mu_filtered)

        # v2.2: Emit continuous vector
        if self.on_vector:
            self.on_vector(dx, dy, label)

        # Legacy discrete callback (for blink-based click or backward compat)
        if self.on_mi and label:
            self.on_mi(label)

    def get_channel_status(self) -> dict:
        """Returns per-channel activity status for GUI display."""
        # v2.3.1 channel labels — CH1/2/9/10 all blink
        names = [
            "CH1(眨眼)",  "CH2(眨眼)",  "CH3(专注)",  "CH4(Aux手L)",
            "CH5(手L★)", "CH6(脚L★)", "CH7(Aux脚L)","CH8(禁用)",
            "CH9(眨眼)",  "CH10(眨眼)", "CH11(专注)", "CH12(Aux手R)",
            "CH13(手R★)","CH14(脚R★)","CH15(Aux脚R)","CH16(禁用)",
        ]
        roles = [
            "blink", "blink", "focus", "aux_hand",
            "hand",  "foot",  "aux_foot", "disabled",
            "blink", "blink", "focus",  "aux_hand",
            "hand",  "foot",  "aux_foot", "disabled",
        ]

        status = {}
        for i in range(16):
            status[i] = {
                "name":     names[i],
                "role":     roles[i],
                "active":   bool(self._channel_activity[i]),
                "disabled": i in DISABLED_CHANNELS,
                "head_top": i in HEAD_TOP_CHANNELS,
            }
        return status
