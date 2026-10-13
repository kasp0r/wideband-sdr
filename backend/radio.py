"""Radio controller: owns the single rtl_tcp connection and the DSP reader thread."""
import logging
import queue
import threading
import time

import numpy as np
from rtltcp import RtlTcpClient, RtlTcpError

import config
import demod
import dsp

log = logging.getLogger(__name__)


class Radio:
    def __init__(self):
        self.sdr = None
        self.lock = threading.Lock()
        self.running = False
        self._thread = None
        self.connected = False

        self.center_freq = config.DEFAULT_FREQ
        self.sample_rate = config.SAMPLE_RATE
        self.gain = config.DEFAULT_GAIN
        self.freq_correction = config.FREQ_CORRECTION

        # connection bookkeeping (for auto-reconnect)
        self.host = config.SDR_HOST
        self.port = config.SDR_PORT
        self._reconnect_attempts = 0

        self.latest_spectrum = None
        self._spectrum_data = None  # full-precision (freqs, power) for the AI helpers
        self._metrics = None  # cached signal metrics (recomputed every N frames)
        self._frame_count = 0
        self.seq = 0

        # audio demodulation
        self.audio_enabled = False
        self.audio_mode = config.DEFAULT_DEMOD
        self.audio_bandwidth = None  # None = use the mode's default
        self.audio_deemphasis = 75e-6  # seconds (FM de-emphasis time constant)
        self.audio_tone = 0.0  # -1..1 tilt EQ (tone control)
        self.audio_bfo = 0.0  # Hz: SSB beat-frequency offset (audio pitch)
        self.audio_nr = 0.0  # Hz: noise-reduction low-pass cutoff (0 = off)
        self.audio_squelch = 0.0  # dB: mute audio below this SNR (0 = off)
        self.audio_queue = queue.Queue(maxsize=64)
        self._demod = None
        self._audio_parts = []
        self._audio_len = 0
        self.audio_chunk = int(config.SAMPLE_RATE * 0.1)

    # ---- lifecycle ----
    def connect(self, host=None, port=None):
        self.host = host or config.SDR_HOST
        self.port = port or config.SDR_PORT
        # Ensure a clean state first (not while holding self.lock).
        self.disconnect()
        self._open_connection()
        self._reconnect_attempts = 0
        self.running = True
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()

    def _open_connection(self):
        with self.lock:
            self.sdr = RtlTcpClient(self.host, self.port)
            self.sdr.set_sample_rate(self.sample_rate)
            self.sdr.set_center_freq(self.center_freq)
            self.sdr.set_freq_correction(self.freq_correction)
            self._apply_gain(self.gain)
            self.connected = True
        log.info("connected to rtl_tcp at %s:%s", self.host, self.port)

    def disconnect(self):
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        with self.lock:
            if self.sdr is not None:
                try:
                    self.sdr.close()
                except Exception:
                    pass
                self.sdr = None
            self.connected = False

    def set_freq_correction(self, ppm):
        self.freq_correction = int(ppm)
        with self.lock:
            if self.sdr is not None:
                self.sdr.set_freq_correction(self.freq_correction)
        return {"ok": True, "freq_correction_ppm": self.freq_correction}

    def _on_disconnect(self, exc):
        log.warning("rtl_tcp connection lost: %s", exc)
        with self.lock:
            self.connected = False
            if self.sdr is not None:
                try:
                    self.sdr.close()
                except Exception:
                    pass
                self.sdr = None

    def _wait(self, delay):
        end = time.time() + delay
        while self.running and time.time() < end:
            time.sleep(0.1)
        return self.running

    def _apply_gain(self, gain):
        if gain == "auto" or gain is None:
            self.sdr.set_gain_mode(False)
        else:
            self.sdr.set_gain(float(gain))

    # ---- control (thread-safe via self.lock) ----
    def tune(self, freq_hz):
        freq_hz = int(freq_hz)
        with self.lock:
            if self.sdr is None:
                return {"error": "not connected"}
            self.sdr.set_center_freq(freq_hz)
            self.center_freq = freq_hz
        return self.status()

    def fine_tune(self, offset_hz):
        return self.tune(self.center_freq + int(offset_hz))

    def set_gain(self, gain):
        with self.lock:
            if self.sdr is None:
                return {"error": "not connected"}
            try:
                if gain == "auto" or gain is None:
                    self.sdr.set_gain_mode(False)
                    self.gain = "auto"
                else:
                    self.sdr.set_gain(float(gain))
                    self.gain = float(gain)
            except Exception as e:
                return {"error": f"gain rejected: {e}"}
        return self.status()

    def set_sample_rate(self, sample_rate):
        sample_rate = int(sample_rate)
        with self.lock:
            if self.sdr is None:
                return {"error": "not connected"}
            self.sdr.set_sample_rate(sample_rate)
            self.sample_rate = sample_rate
            self.audio_chunk = int(sample_rate * 0.1)
        return self.status()

    # ---- audio ----
    def set_audio_mode(self, mode):
        if mode in demod.VALID_MODES:
            self.audio_mode = mode
            self.audio_bandwidth = None  # reset to the mode's default
            self._demod = None  # recreate lazily

    def set_audio_bandwidth(self, bandwidth):
        self.audio_bandwidth = int(bandwidth)
        self._demod = None  # recreate lazily

    def set_audio_deemphasis(self, deemphasis):
        tau_map = {"off": 0.0, "50us": 50e-6, "75us": 75e-6}
        self.audio_deemphasis = tau_map.get(deemphasis, 75e-6)
        self._demod = None  # recreate lazily

    def set_audio_tone(self, tone):
        self.audio_tone = float(tone)
        self._demod = None  # recreate lazily

    def set_audio_bfo(self, bfo):
        self.audio_bfo = float(bfo)
        self._demod = None  # recreate lazily

    def set_audio_nr(self, cutoff):
        self.audio_nr = float(cutoff)
        self._demod = None  # recreate lazily

    def set_audio_squelch(self, threshold):
        self.audio_squelch = float(threshold)

    def effective_bandwidth(self):
        if self.audio_bandwidth is not None:
            return self.audio_bandwidth
        return demod.DEFAULT_BANDWIDTH.get(self.audio_mode, 16_000)

    def set_audio_enabled(self, enabled):
        self.audio_enabled = bool(enabled)
        if not self.audio_enabled:
            self._audio_parts = []
            self._audio_len = 0

    def _squelch_open(self):
        m = self._metrics
        if not m or not m.get("signal_detected"):
            return False
        return float(m.get("snr_db", 0.0)) >= self.audio_squelch

    def _process_audio(self, iq):
        if (self._demod is None or self._demod.sample_rate != self.sample_rate
                or self._demod.mode != self.audio_mode
                or (self.audio_bandwidth is not None
                    and self._demod.bandwidth != self.audio_bandwidth)
                or self._demod.deemphasis_tau != self.audio_deemphasis
                or self._demod.tone != self.audio_tone
                or self._demod.bfo != self.audio_bfo
                or self._demod.nr_cutoff != self.audio_nr):
            self._demod = demod.Demodulator(
                self.sample_rate, self.audio_mode, self.audio_bandwidth,
                deemphasis_tau=self.audio_deemphasis,
                tone=self.audio_tone,
                bfo=self.audio_bfo,
                nr_cutoff=self.audio_nr,
            )
        self._audio_parts.append(iq)
        self._audio_len += len(iq)
        if self._audio_len >= self.audio_chunk:
            chunk = np.concatenate(self._audio_parts)
            self._audio_parts = []
            self._audio_len = 0
            audio = self._demod.process(chunk, 0.0)
            # Squelch: mute the audio when the tuned signal's SNR is below threshold.
            if self.audio_squelch > 0 and not self._squelch_open():
                audio = np.zeros_like(audio)
            if len(audio):
                try:
                    self.audio_queue.put_nowait(audio)
                except queue.Full:
                    try:
                        self.audio_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.audio_queue.put_nowait(audio)
                    except queue.Full:
                        pass

    def status(self):
        return {
            "connected": self.connected,
            "center_hz": self.center_freq,
            "sample_rate": self.sample_rate,
            "gain": str(self.gain),
            "freq_correction_ppm": self.freq_correction,
        }

    # ---- spectrum reader ----
    def _reader_loop(self):
        n = config.FFT_SIZE * config.AVERAGES
        while self.running:
            # Reconnect (with exponential backoff) if there is no connection.
            while self.running and self.sdr is None:
                delay = min(config.RECONNECT_DELAY * (2 ** self._reconnect_attempts),
                            config.RECONNECT_MAX_DELAY)
                self._reconnect_attempts += 1
                if not self._wait(delay):
                    return
                try:
                    self._open_connection()
                    self._reconnect_attempts = 0
                except Exception as e:
                    log.warning("connect attempt failed: %s", e)

            if not self.running:
                return

            try:
                with self.lock:
                    if self.sdr is None:
                        continue
                    raw = self.sdr.read_bytes(n * 2)
                    cf = self.center_freq
                iq = dsp.to_complex(np.frombuffer(raw, dtype=np.uint8))
                freqs, power = dsp.compute_averaged_spectrum(
                    iq, self.sample_rate, config.FFT_SIZE, cf, config.AVERAGES
                )
                self._spectrum_data = (freqs, power)
                # Recompute signal metrics only every METRICS_INTERVAL frames —
                # the inspector/AI don't need 30 Hz updates.
                self._frame_count += 1
                if self._metrics is None or self._frame_count % config.METRICS_INTERVAL == 0:
                    self._metrics = dsp.signal_metrics(freqs, power, cf)
                self.latest_spectrum = {
                    "type": "spectrum",
                    "center_hz": cf,
                    "sample_rate": self.sample_rate,
                    "min_span_hz": config.MIN_SPAN_HZ,
                    "demod_mode": self.audio_mode,
                    "bandwidth": self.effective_bandwidth(),
                    "band": dsp.band_context(cf),
                    "metrics": self._metrics,
                    # freqs is reconstructed client-side; power rounded to keep
                    # the per-frame JSON payload small at high FFT sizes.
                    "power": np.round(power, 2).tolist(),
                }
                self.seq += 1
                if self.audio_enabled:
                    self._process_audio(iq)
            except (RtlTcpError, OSError) as e:
                # Connection dropped: mark disconnected and reconnect.
                self._on_disconnect(e)
            except Exception as e:
                log.warning("transient reader error: %s", e)
                time.sleep(0.05)

    # ---- AI-facing helpers ----
    def get_signal_metrics(self):
        if self._spectrum_data is None:
            return {"error": "no spectrum data yet"}
        freqs, power = self._spectrum_data
        return dsp.signal_metrics(freqs, power, self.center_freq)

    def spectrum_snapshot(self, top_n=20, threshold_db=10.0):
        """Text-friendly summary of current spectrum: strongest peaks."""
        if self._spectrum_data is None:
            return {"error": "no spectrum data yet"}
        freqs, power = self._spectrum_data
        peaks, noise_floor = dsp.find_peaks(freqs, power, threshold_db)
        peaks.sort(key=lambda p: -p["power_db"])
        for p in peaks[:top_n]:
            p["modulation_hint"] = dsp.guess_modulation(p["bandwidth_hz"])
            p["band"] = dsp.band_context(p["freq_hz"])
        return {
            "center_hz": self.center_freq,
            "sample_rate": self.sample_rate,
            "noise_floor_db": noise_floor,
            "num_signals": len(peaks),
            "peaks": peaks[:top_n],
        }

    def characterize(self, freq_hz=None):
        """Analyze a single signal near freq_hz (retuning if outside current span)."""
        freq_hz = freq_hz if freq_hz is not None else self.center_freq
        if not self.connected:
            return {"error": "not connected"}
        span = self.sample_rate
        if abs(freq_hz - self.center_freq) > span / 2:
            self.tune(freq_hz)
            time.sleep(0.15)
        snap = self.spectrum_snapshot(top_n=200, threshold_db=6.0)
        peaks = snap.get("peaks", [])
        if not peaks:
            return {"freq_hz": freq_hz, "note": "no signal detected nearby"}
        best = min(peaks, key=lambda p: abs(p["freq_hz"] - freq_hz))
        return best

    def scan(self, start_hz, stop_hz, step_hz=None, dwell=0.15, max_steps=1000):
        """Sweep [start_hz, stop_hz) and collect signals, restoring tuning after."""
        if not self.connected:
            return {"error": "not connected"}
        start_hz, stop_hz = int(start_hz), int(stop_hz)
        if stop_hz <= start_hz:
            start_hz, stop_hz = stop_hz, start_hz

        # A step smaller than the receiver span would just re-tune over already
        # covered spectrum, so floor it at half the sample rate. This also makes
        # a stray 0/negative step from the model impossible.
        min_step = int(max(self.sample_rate * 0.5, 1_000))
        step = int(step_hz) if step_hz else min_step
        if step < min_step:
            step = min_step

        original = self.center_freq
        found = []
        truncated = False
        steps = 0
        freq = start_hz
        while freq < stop_hz:
            if steps >= max_steps:
                truncated = True
                break
            self.tune(freq)
            time.sleep(dwell)
            snap = self.spectrum_snapshot(top_n=10, threshold_db=10.0)
            for p in snap.get("peaks", []):
                if start_hz <= p["freq_hz"] < stop_hz:
                    p["modulation_hint"] = dsp.guess_modulation(p["bandwidth_hz"])
                    p["band"] = dsp.band_context(p["freq_hz"])
                    found.append(p)
            freq += step
            steps += 1
        # restore
        self.tune(original)
        # de-duplicate by frequency bin
        dedup = {}
        for p in found:
            key = int(p["freq_hz"] // (self.sample_rate / 8))
            if key not in dedup or p["power_db"] > dedup[key]["power_db"]:
                dedup[key] = p
        signals = sorted(dedup.values(), key=lambda p: -p["power_db"])
        return {
            "scanned_hz": [start_hz, stop_hz],
            "step_hz": step,
            "num_signals": len(signals),
            "signals": signals,
            "restored_center_hz": original,
            "truncated": truncated,
        }
