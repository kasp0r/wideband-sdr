"""Real-time demodulators: AM, NBFM, WFM, USB, LSB -> mono audio at 48 kHz.

The reader thread hands us blocks of baseband IQ at the SDR sample rate; we
frequency-shift the target signal to DC, decimate, and demodulate to audio.
Chunks are processed with scipy's polyphase resampler (fast, stateless).

All post-detection filtering is *causal and stateful*: each filter carries its
state (zi) across process() calls, so there are no edge transients or clicks at
chunk boundaries. (``filtfilt`` is zero-phase but non-causal and therefore
unsuitable for streaming; the stateful ``lfilter`` below introduces only a small,
fixed group delay, which is inaudible.)
"""
from math import gcd

import numpy as np
from scipy import signal

AUDIO_RATE = 48000
VALID_MODES = ("am", "nbfm", "wfm", "usb", "lsb")

# Default IF filter bandwidth (Hz) per mode: the full width of the passband
# shown in the UI. The audio low-pass cutoff is derived from this.
DEFAULT_BANDWIDTH = {
    "am": 16_000,
    "nbfm": 16_000,
    "wfm": 200_000,
    "usb": 2_700,
    "lsb": 2_700,
}


def _resample(x, fs, target_fs):
    """Rate-convert x from fs to target_fs (anti-alias at the output Nyquist)."""
    if fs == target_fs:
        return x
    g = gcd(int(round(fs)), int(round(target_fs)))
    up = int(round(target_fs)) // g
    down = int(round(fs)) // g
    return signal.resample_poly(x, up, down)


class _StatefulFilter:
    """A causal linear filter that carries its state (zi) across chunks.

    Replaces ``filtfilt`` so that chunked streaming produces a continuous output
    with no edge transients. Uses ``lfilter`` with an explicit ``zi`` state that
    is threaded through successive calls.
    """

    def __init__(self, b, a=(1.0,)):
        self.b = np.asarray(b, dtype=np.float64)
        self.a = np.asarray(a, dtype=np.float64)
        self.reset()

    def reset(self):
        n = max(len(self.b), len(self.a)) - 1
        self.zi = np.zeros(n, dtype=np.float64)

    def __call__(self, x):
        y, self.zi = signal.lfilter(self.b, self.a, x, zi=self.zi)
        return y


_TAP_CACHE = {}  # (fs, cutoff) -> firwin taps


def _fir_taps(fs, cutoff):
    """Cache firwin taps keyed by (fs, cutoff); they change rarely."""
    key = (int(fs), round(float(cutoff), 2))
    taps = _TAP_CACHE.get(key)
    if taps is None:
        taps = signal.firwin(64, cutoff, fs=fs)
        _TAP_CACHE[key] = taps
    return taps


def _deemphasize(x, fs, tau):
    """One-pole FM de-emphasis low-pass with time constant tau (seconds).

    Stateless convenience helper (kept for tests/tools). The demodulator uses a
    stateful equivalent so the filter state carries across chunks.
    """
    if tau <= 0 or len(x) < 4:
        return x
    alpha = float(np.exp(-1.0 / (fs * tau)))
    return signal.lfilter([1.0 - alpha], [1.0, -alpha], x)


class Demodulator:
    def __init__(self, sample_rate, mode="wfm", bandwidth=None, deemphasis_tau=75e-6, tone=0.0, bfo=0.0, nr_cutoff=0.0):
        self.sample_rate = int(sample_rate)
        self.mode = mode
        self.bandwidth = int(bandwidth) if bandwidth else DEFAULT_BANDWIDTH.get(mode, 16_000)
        self.deemphasis_tau = deemphasis_tau
        self.tone = float(tone)
        self.bfo = float(bfo)  # SSB beat-frequency offset (Hz): shifts the audio pitch
        self.nr_cutoff = float(nr_cutoff)  # audio low-pass cutoff (Hz): 0 = off
        self.audio_rate = AUDIO_RATE
        self._sample_pos = 0  # cumulative samples, keeps the NCO phase continuous
        self._peak = None     # smoothed audio peak for AGC
        self._filters = {}    # (kind, ...) -> _StatefulFilter

    def set_bandwidth(self, bandwidth):
        self.bandwidth = int(bandwidth)

    def set_deemphasis(self, tau):
        self.deemphasis_tau = tau

    def set_tone(self, tone):
        self.tone = float(tone)

    def set_bfo(self, bfo):
        self.bfo = float(bfo)

    def set_nr(self, cutoff):
        self.nr_cutoff = float(cutoff)

    def _audio_cutoff(self):
        b = self.bandwidth
        if self.mode in ("usb", "lsb"):
            return b
        if self.mode == "wfm":
            return min(b / 2.0, 15_000.0)
        return b / 2.0  # AM, NBFM (double-sideband)

    def _lp(self, x, fs, cutoff):
        """Stateful causal low-pass. Returns x unchanged if no filtering is needed."""
        if cutoff >= fs / 2 or len(x) < 8:
            return x
        key = ("lp", int(fs), round(float(cutoff), 2))
        f = self._filters.get(key)
        if f is None:
            f = _StatefulFilter(_fir_taps(fs, cutoff))
            self._filters[key] = f
        return f(x)

    def _deemph(self, x, fs, tau):
        """Stateful one-pole FM de-emphasis."""
        if tau <= 0 or len(x) < 4:
            return x
        key = ("deemph", int(fs), round(float(tau) * 1e6, 3))
        f = self._filters.get(key)
        if f is None:
            alpha = float(np.exp(-1.0 / (fs * tau)))
            f = _StatefulFilter([1.0 - alpha], [1.0, -alpha])
            self._filters[key] = f
        return f(x)

    def process(self, iq, offset_hz=0.0):
        """Return float32 mono audio (approx [-1, 1]) for this block.

        ``iq`` is a complex array at ``self.sample_rate``; ``offset_hz`` is the
        signal's offset from the SDR center frequency.
        """
        fs = self.sample_rate
        n = len(iq)
        # Continuous local oscillator (phase carried across blocks).
        t = (np.arange(n) + self._sample_pos) / fs
        self._sample_pos += n
        x = np.asarray(iq, dtype=np.complex128)
        # Bring the target to the demodulator input. For SSB, a BFO offset shifts
        # the recovered audio's pitch (positive = higher, for both USB and LSB).
        shift = offset_hz
        if self.mode == "usb":
            shift -= self.bfo
        elif self.mode == "lsb":
            shift += self.bfo
        if shift:
            x = x * np.exp(-2j * np.pi * shift * t)

        cutoff = self._audio_cutoff()
        if self.mode == "wfm":
            audio = self._fm(x, fs, rf_rate=240_000, audio_cutoff=cutoff)
        elif self.mode == "nbfm":
            audio = self._fm(x, fs, rf_rate=48_000, audio_cutoff=cutoff)
        elif self.mode == "am":
            audio = self._am(x, fs, audio_cutoff=cutoff)
        elif self.mode in ("usb", "lsb"):
            audio = self._ssb(x, fs, audio_cutoff=cutoff)
        else:
            audio = np.zeros(0, dtype=np.float64)

        audio = self._tone(audio)
        # Noise reduction: an extra audio low-pass to roll off high-frequency hiss.
        if self.nr_cutoff > 0:
            audio = self._lp(audio, self.audio_rate, self.nr_cutoff)
        return self._normalize(audio).astype(np.float32)

    # ---- demod primitives ----
    def _fm(self, x, fs, rf_rate, audio_cutoff):
        # Bring the FM signal to an intermediate rate that still holds its
        # full deviation, then take the instantaneous frequency.
        xrf = _resample(x, fs, rf_rate)
        inst_freq = np.diff(np.unwrap(np.angle(xrf))) * (rf_rate / (2.0 * np.pi))
        inst_freq = self._lp(inst_freq, rf_rate, audio_cutoff)
        audio = _resample(inst_freq, rf_rate, self.audio_rate)
        return self._deemph(audio, self.audio_rate, self.deemphasis_tau)

    def _am(self, x, fs, audio_cutoff):
        xr = _resample(x, fs, self.audio_rate)
        env = np.abs(xr)
        env = self._lp(env, self.audio_rate, audio_cutoff)
        return env - np.mean(env)

    def _ssb(self, x, fs, audio_cutoff):
        # With the (suppressed) carrier placed at DC, the sideband is analytic
        # (USB, positive freqs) or anti-analytic (LSB, negative freqs); the real
        # part recovers the message. A BFO offset is applied upstream in process().
        xr = _resample(x, fs, self.audio_rate)
        audio = np.real(xr)
        return self._lp(audio, self.audio_rate, audio_cutoff)

    def _tone(self, audio):
        """Tilt EQ: tone in [-1, 1] tilts the spectrum around ~1 kHz (±6 dB)."""
        if abs(self.tone) < 1e-6 or len(audio) < 8:
            return audio
        alpha = 1.0 - np.exp(-2.0 * np.pi * 1000.0 / self.audio_rate)
        f = self._filters.get(("tone",))
        if f is None:
            f = _StatefulFilter([alpha], [1.0, -(1.0 - alpha)])
            self._filters[("tone",)] = f
        low = f(audio)
        high = audio - low
        g = 10.0 ** (self.tone * 6.0 / 20.0)
        return low / g + high * g

    def _normalize(self, audio):
        if len(audio) == 0:
            return audio
        audio = audio - np.mean(audio)
        p = float(np.max(np.abs(audio))) + 1e-9
        # Smooth AGC to avoid per-chunk volume pumping.
        self._peak = p if self._peak is None else 0.95 * self._peak + 0.05 * p
        scaled = audio * (0.8 / self._peak)
        # Soft limiter (tanh) instead of hard clipping: transients compress
        # smoothly to +/-1 rather than clipping sharply.
        return np.tanh(scaled)
