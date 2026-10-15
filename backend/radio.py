"""Radio controller: owns the single rtl_tcp connection and the DSP threads.

Parallelism model
-----------------
The single biggest CPU cost is socket I/O: ``recv()`` data-copy time dwarfs the
FFT and audio demodulation. The reader thread therefore does as little as
possible — it reads IQ from the socket and posts the raw block to a lightweight
``queue.Queue`` consumed by the audio thread. The spectrum (FFT) is computed
inline in the reader (only ~1 ms/frame and it must be low-latency for the UI).

Audio demodulation runs in a **dedicated thread**, not a process. NumPy/SciPy
release the GIL for their heavy kernels (FFT, resample_poly, lfilter), so the
audio thread genuinely runs in parallel with the reader thread and the asyncio
broadcast loops — without the pickling/pipe overhead of ``multiprocessing``
queues, which were measured to *slow* the reader enough to stall rtl_tcp
(receive-window-limited, 3.8% TCP retransmits).

    Reader thread:  socket read → to_complex → FFT → latest_spectrum
                        └────────────── IQ ──────► audio queue (queue.Queue)
    Audio thread:   accumulate IQ → demodulate → audio_queue (queue.Queue)
    Asyncio loop:   broadcast latest_spectrum + audio_queue → WebSockets

Threading contract:
- The reader thread (``_reader_loop``) is the only thread that touches ``sdr``
  (socket I/O), ``_spectrum_data``, ``_metrics``, and ``latest_spectrum``.
- The audio thread (``_audio_loop``) reads IQ from ``_audio_iq_queue`` and writes
  demodulated audio to ``audio_queue``; both are thread-safe ``queue.Queue``.
- Request/API threads mutate control settings (``center_freq``, ``gain``,
  ``sample_rate``, audio parameters) under ``self.lock``. The reader/audio threads
  read the same settings without the lock; they are plain value assignments, so
  a stale value for one frame is harmless.
"""
import logging
import queue
import struct
import subprocess
import sys
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
        self._run_id = 0
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
        self._scan_cancel = threading.Event()  # cooperative scan/characterize cancellation

        # audio demodulation (runs in a separate subprocess, not a thread).
        self.audio_enabled = False
        self.audio_mode = config.DEFAULT_DEMOD
        self.audio_bandwidth = None  # None = use the mode's default
        self.audio_deemphasis = 75e-6  # seconds (FM de-emphasis time constant)
        self.audio_tone = 0.0  # -1..1 tilt EQ (tone control)
        self.audio_bfo = 0.0  # Hz: SSB beat-frequency offset (audio pitch)
        self.audio_nr = 0.0  # Hz: noise-reduction low-pass cutoff (0 = off)
        self.audio_squelch = 0.0  # dB: mute audio below this SNR (0 = off)
        self.audio_chunk = int(config.SAMPLE_RATE * 0.05)

        # Spectrum thread (FFT is fast ~1 ms; keep in-thread for low latency).
        # Audio demod runs in a SUBPROCESS (audio_worker.py) to bypass the GIL.
        self._spectrum_raw_queue = queue.Queue(maxsize=32)  # reader → spectrum thread (bytes)
        self._audio_raw_queue = queue.Queue(maxsize=64)      # reader → writer thread (bytes)
        self.audio_queue = queue.Queue(maxsize=64)          # stdout thread → asyncio (float32)
        self._spectrum_thread = None
        self._spectrum_stop = threading.Event()
        self._audio_proc = None        # subprocess.Popen for audio_worker.py
        self._audio_writer_thread = None
        self._audio_stdout_thread = None
        self._audio_stop = threading.Event()
        self._audio_restart_timer = None
        self._diag_reads = 0
        self._diag_audio_chunks = 0
        self._diag_spec_frames = 0
        self._diag_drops = 0
        self._diag_t0 = time.time()

    # ---- lifecycle ----
    def connect(self, host=None, port=None):
        self.host = host or config.SDR_HOST
        self.port = port or config.SDR_PORT
        # Ensure a clean state first (not while holding self.lock).
        self.disconnect()
        self._open_connection()
        self._reconnect_attempts = 0
        self.running = True
        self._run_id += 1
        run_id = self._run_id
        self._start_spectrum_thread()
        self._thread = threading.Thread(target=self._reader_loop, args=(run_id,), daemon=True)
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
        self._scan_cancel.set()  # abort any in-flight scan/characterize sweep
        self._run_id += 1  # invalidate any in-flight reader loop so it exits
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

        # Stop the audio subprocess.
        self._stop_audio_worker()

        # Stop the spectrum thread.
        self._spectrum_stop.set()
        try:
            self._spectrum_raw_queue.put_nowait(None)  # wake the spectrum thread
        except queue.Full:
            pass
        if self._spectrum_thread is not None:
            self._spectrum_thread.join(timeout=1.0)
            self._spectrum_thread = None
        self._spectrum_stop.clear()

        # Drain any stale queues.
        for q in (self._spectrum_raw_queue, self.audio_queue):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

        with self.lock:
            if self.sdr is not None:
                try:
                    self.sdr.close()
                except Exception:
                    pass
                self.sdr = None
            self.connected = False

    def cancel_scan(self):
        """Request cooperative cancellation of any in-flight scan()/characterize()."""
        self._scan_cancel.set()

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

    def _scan_wait(self, seconds):
        """Sleep in short slices, returning False early if a scan is cancelled."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._scan_cancel.is_set():
                return False
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
        return True

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
        if gain == "auto" or gain is None:
            manual = False
            gain_value = None
        else:
            try:
                gain_value = float(gain)
            except (TypeError, ValueError):
                return {"error": f"invalid gain: {gain!r}"}
            if not (config.MIN_GAIN_DB <= gain_value <= config.MAX_GAIN_DB):
                return {"error": f"gain {gain_value} dB out of range "
                                 f"[{config.MIN_GAIN_DB}, {config.MAX_GAIN_DB}]"}
            manual = True
        with self.lock:
            if self.sdr is None:
                return {"error": "not connected"}
            try:
                if manual:
                    self.sdr.set_gain(gain_value)
                    self.gain = gain_value
                else:
                    self.sdr.set_gain_mode(False)
                    self.gain = "auto"
            except Exception as e:
                return {"error": f"gain rejected: {e}"}
        return self.status()

    def set_sample_rate(self, sample_rate):
        sample_rate = int(sample_rate)
        if not (config.MIN_SAMPLE_RATE <= sample_rate <= config.MAX_SAMPLE_RATE):
            return {"error": f"sample rate {sample_rate} Hz out of range "
                             f"[{config.MIN_SAMPLE_RATE}, {config.MAX_SAMPLE_RATE}]"}
        with self.lock:
            if self.sdr is None:
                return {"error": "not connected"}
            self.sdr.set_sample_rate(sample_rate)
            self.sample_rate = sample_rate
            self.audio_chunk = int(sample_rate * 0.05)
        return self.status()

    # ---- audio ----
    def set_audio_mode(self, mode):
        if mode in demod.VALID_MODES:
            self.audio_mode = mode
            self.audio_bandwidth = None  # reset to the mode's default

    def set_audio_bandwidth(self, bandwidth):
        self.audio_bandwidth = int(bandwidth)

    def set_audio_deemphasis(self, deemphasis):
        tau_map = {"off": 0.0, "50us": 50e-6, "75us": 75e-6}
        self.audio_deemphasis = tau_map.get(deemphasis, 75e-6)

    def set_audio_tone(self, tone):
        self.audio_tone = float(tone)

    def set_audio_bfo(self, bfo):
        self.audio_bfo = float(bfo)

    def set_audio_nr(self, cutoff):
        self.audio_nr = float(cutoff)

    def set_audio_squelch(self, threshold):
        # Squelch is applied in the main process (stdout loop), no restart needed.
        self.audio_squelch = float(threshold)

    def commit_audio_config(self):
        """Apply pending audio settings by (re)starting the worker subprocess.

        Debounced: slider "input" events fire many times per second while
        dragging; each would otherwise spawn a fresh subprocess (~2-3 s SciPy
        import) and block the event loop on the old one's shutdown. We coalesce
        rapid changes into a single restart ~300 ms after the user pauses, and
        run it on a background thread so the UI never freezes.
        """
        if not self.audio_enabled:
            return
        if self._audio_restart_timer is not None:
            self._audio_restart_timer.cancel()
        self._audio_restart_timer = threading.Timer(0.3, self._restart_audio_worker)
        self._audio_restart_timer.daemon = True
        self._audio_restart_timer.start()

    def _restart_audio_worker(self):
        self._audio_restart_timer = None
        if self.audio_enabled:
            self._start_audio_worker()

    def effective_bandwidth(self):
        if self.audio_bandwidth is not None:
            return self.audio_bandwidth
        return demod.DEFAULT_BANDWIDTH.get(self.audio_mode, 16_000)

    def set_audio_enabled(self, enabled):
        was_enabled = self.audio_enabled
        self.audio_enabled = bool(enabled)
        if self.audio_enabled and not was_enabled:
            self._start_audio_worker()
        elif not self.audio_enabled and was_enabled:
            self._stop_audio_worker()

    def _start_spectrum_thread(self):
        self._stop_spectrum_thread()
        self._spectrum_stop.clear()
        self._spectrum_thread = threading.Thread(target=self._spectrum_loop, daemon=True)
        self._spectrum_thread.start()

    def _stop_spectrum_thread(self):
        self._spectrum_stop.set()
        try:
            self._spectrum_raw_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._spectrum_thread is not None:
            self._spectrum_thread.join(timeout=1.0)
            self._spectrum_thread = None
        self._spectrum_stop.clear()
        while True:
            try:
                self._spectrum_raw_queue.get_nowait()
            except queue.Empty:
                break

    def _start_audio_worker(self):
        """Spawn the audio demod subprocess and its stdout reader thread."""
        self._stop_audio_worker()
        self._audio_stop.clear()
        cmd = [
            sys.executable, "audio_worker.py",
            str(self.sample_rate),
            self.audio_mode,
            str(self.audio_bandwidth or ""),
            str(self.audio_deemphasis),
            str(self.audio_tone),
            str(self.audio_bfo),
            str(self.audio_nr),
        ]
        try:
            self._audio_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log.warning("failed to start audio worker: %s", e)
            self._audio_proc = None
            return
        # Writer thread: moves raw IQ from a queue to the subprocess stdin. This
        # keeps the reader thread from ever blocking on a full pipe (the worker
        # pauses ~14 ms during demod and can't drain stdin in that window).
        self._audio_writer_thread = threading.Thread(target=self._audio_writer_loop, daemon=True)
        self._audio_writer_thread.start()
        # Stdout reader thread: parses length-prefixed audio frames from stdout.
        self._audio_stdout_thread = threading.Thread(target=self._audio_stdout_loop, daemon=True)
        self._audio_stdout_thread.start()

    def _stop_audio_worker(self):
        """Stop the audio subprocess and its reader/writer threads."""
        self._audio_stop.set()
        # Cancel any pending (debounced) restart.
        if self._audio_restart_timer is not None:
            self._audio_restart_timer.cancel()
            self._audio_restart_timer = None
        # Wake the writer thread so it exits.
        try:
            self._audio_raw_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._audio_proc is not None:
            proc = self._audio_proc
            self._audio_proc = None
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self._audio_writer_thread is not None:
            self._audio_writer_thread.join(timeout=1.0)
            self._audio_writer_thread = None
        if self._audio_stdout_thread is not None:
            self._audio_stdout_thread.join(timeout=1.0)
            self._audio_stdout_thread = None
        self._audio_stop.clear()
        # Drain stale queues.
        while True:
            try:
                self._audio_raw_queue.get_nowait()
            except queue.Empty:
                break
        while True:
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break

    def _audio_writer_loop(self):
        """Move raw IQ from the queue into the subprocess stdin (may block)."""
        while not self._audio_stop.is_set():
            try:
                raw = self._audio_raw_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if raw is None:
                break
            proc = self._audio_proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                break
            try:
                proc.stdin.write(raw)
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                break  # subprocess died

    def _audio_stdout_loop(self):
        """Read length-prefixed audio frames from the subprocess stdout.

        The worker writes ``[uint32 LE length][float32 audio]`` per chunk; this
        thread parses those frames, applies squelch, and posts to ``audio_queue``
        (drained by the asyncio broadcast loop).
        """
        proc = self._audio_proc
        if proc is None:
            return
        stdout = proc.stdout
        while not self._audio_stop.is_set():
            try:
                header = stdout.read(4)
            except Exception:
                break
            if len(header) < 4:
                break
            (length,) = struct.unpack("<I", header)
            try:
                data = stdout.read(length)
            except Exception:
                break
            if len(data) < length:
                break
            audio = np.frombuffer(data, dtype="<f4")
            # Squelch: mute the audio when the tuned signal's SNR is below threshold.
            if self.audio_squelch > 0 and not self._squelch_open():
                audio = np.zeros_like(audio)
            if len(audio):
                self._diag_audio_chunks += 1
                try:
                    self.audio_queue.put_nowait(audio)
                except queue.Full:
                    # Drop oldest, push newest — realtime audio can't wait.
                    try:
                        self.audio_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.audio_queue.put_nowait(audio)
                    except queue.Full:
                        pass

    def _squelch_open(self):
        m = self._metrics
        if not m or not m.get("signal_detected"):
            return False
        return float(m.get("snr_db", 0.0)) >= self.audio_squelch

    def _spectrum_loop(self):
        """Spectrum computation thread.

        Consumes raw IQ bytes from the reader, computes the FFT, and updates
        ``latest_spectrum``. Runs in parallel with the audio thread (NumPy
        releases the GIL for the FFT).
        """
        while not self._spectrum_stop.is_set():
            try:
                raw = self._spectrum_raw_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if raw is None:
                break

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
            self._diag_spec_frames += 1
            self.latest_spectrum = {
                "type": "spectrum",
                "center_hz": cf,
                "sample_rate": self.sample_rate,
                "min_span_hz": config.MIN_SPAN_HZ,
                "demod_mode": self.audio_mode,
                "bandwidth": self.effective_bandwidth(),
                "band": dsp.band_context(cf),
                "metrics": self._metrics,
                # freqs is reconstructed client-side; power is max-pooled to
                # SPECTRUM_BINS then rounded to keep the JSON payload small.
                "power": np.round(dsp.max_pool(power, config.SPECTRUM_BINS), 2).tolist(),
            }
            self.seq += 1

    def status(self):
        dt = max(time.time() - self._diag_t0, 1e-6)
        return {
            "connected": self.connected,
            "center_hz": self.center_freq,
            "sample_rate": self.sample_rate,
            "gain": str(self.gain),
            "freq_correction_ppm": self.freq_correction,
            "_diag": {
                "reads": self._diag_reads,
                "read_rate": round(self._diag_reads / dt, 1),
                "spec_frames": self._diag_spec_frames,
                "spec_rate": round(self._diag_spec_frames / dt, 1),
                "audio_chunks": self._diag_audio_chunks,
                "audio_rate": round(self._diag_audio_chunks / dt, 1),
                "drops": self._diag_drops,
            },
        }

    # ---- spectrum reader ----
    def _reader_loop(self, run_id):
        n = config.FFT_SIZE * config.AVERAGES
        while self.running and self._run_id == run_id:
            # Reconnect (with exponential backoff) if there is no connection.
            while self.running and self._run_id == run_id and self.sdr is None:
                delay = min(config.RECONNECT_DELAY * (2 ** self._reconnect_attempts),
                            config.RECONNECT_MAX_DELAY)
                self._reconnect_attempts += 1
                if not self._wait(delay) or self._run_id != run_id:
                    return
                try:
                    self._open_connection()
                    self._reconnect_attempts = 0
                except Exception as e:
                    log.warning("connect attempt failed: %s", e)

            if not self.running or self._run_id != run_id:
                return

            try:
                with self.lock:
                    sdr = self.sdr
                if sdr is None:
                    continue
                # Do the blocking socket read outside self.lock so that control
                # requests (tune/gain/sample_rate/...) aren't serialized behind a
                # multi-ms read and can reach rtl_tcp immediately.
                # The reader does the MINIMUM work possible (just queue posts) so
                # it drains the socket fast enough to keep rtl_tcp flowing.
                raw = sdr.read_bytes(n * 2)
                self._diag_reads += 1
                try:
                    self._spectrum_raw_queue.put_nowait(raw)
                except queue.Full:
                    self._diag_drops += 1  # spectrum thread is behind; drop this frame
                if self.audio_enabled:
                    try:
                        self._audio_raw_queue.put_nowait(raw)
                    except queue.Full:
                        self._diag_drops += 1  # audio writer is behind; drop this frame
            except (RtlTcpError, OSError) as e:
                if self._run_id != run_id:
                    return  # superseded by a newer connect()/disconnect()
                # Connection dropped: mark disconnected and reconnect.
                self._on_disconnect(e)
            except Exception as e:
                if self._run_id != run_id:
                    return
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
        self._scan_cancel.clear()
        span = self.sample_rate
        if abs(freq_hz - self.center_freq) > span / 2:
            self.tune(freq_hz)
            if not self._scan_wait(0.15):
                return {"freq_hz": freq_hz, "cancelled": True}
        if self._scan_cancel.is_set():
            return {"freq_hz": freq_hz, "cancelled": True}
        snap = self.spectrum_snapshot(top_n=200, threshold_db=6.0)
        peaks = snap.get("peaks", [])
        if not peaks:
            return {"freq_hz": freq_hz, "note": "no signal detected nearby"}
        best = min(peaks, key=lambda p: abs(p["freq_hz"] - freq_hz))
        return best

    def scan(self, start_hz, stop_hz, step_hz=None, dwell=0.15, max_steps=1000,
             max_wall_time=None):
        """Sweep [start_hz, stop_hz) and collect signals, restoring tuning after.

        Cancellation is cooperative: ``cancel_scan()`` (or a disconnect) sets a
        flag checked each step and during each dwell slice. A hard wall-clock cap
        (``max_wall_time``) additionally bounds the sweep even if cancellation is
        never requested, so a single call can't tie up a worker thread forever.
        """
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

        self._scan_cancel.clear()
        deadline = time.monotonic() + (max_wall_time or config.SCAN_MAX_WALL_TIME)

        original = self.center_freq
        found = []
        truncated = False
        cancelled = False
        steps = 0
        freq = start_hz
        while freq < stop_hz:
            if self._scan_cancel.is_set():
                cancelled = True
                break
            if steps >= max_steps or time.monotonic() >= deadline:
                truncated = True
                break
            self.tune(freq)
            if not self._scan_wait(dwell):
                cancelled = True
                break
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
            "cancelled": cancelled,
        }
