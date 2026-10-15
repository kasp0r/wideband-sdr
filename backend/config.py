"""Central configuration, overridable via environment variables."""
import os


def _int(name, default):
    return int(os.environ.get(name, default))


def _float(name, default):
    return float(os.environ.get(name, default))


# ---- SDR / rtl_tcp target ----
SDR_HOST = os.environ.get("SDR_HOST", "127.0.0.1")
SDR_PORT = _int("SDR_PORT", 1234)
SAMPLE_RATE = _int("SAMPLE_RATE", 2_400_000)
FFT_SIZE = _int("FFT_SIZE", 8192)
AVERAGES = _int("AVERAGES", 1)          # FFT frames averaged per update (1 = no averaging)
MIN_SPAN_HZ = _int("MIN_SPAN_HZ", 250_000)  # smallest zoom span in the UI
DEFAULT_FREQ = _int("DEFAULT_FREQ", 100_000_000)  # 100 MHz
DEFAULT_GAIN = os.environ.get("DEFAULT_GAIN", "auto")
DEFAULT_DEMOD = os.environ.get("DEFAULT_DEMOD", "wfm")
AUTO_CONNECT = os.environ.get("AUTO_CONNECT", "1") not in ("0", "false", "False")

# RTL-SDR crystal correction in ppm (0 = none).
FREQ_CORRECTION = _int("FREQ_CORRECTION", 0)

# ---- Hardware limits (RTL-SDR) ----
# Sample-rate range accepted by librtlsdr (225001–300000 and 900001–3200000).
# The UI exposes a subset; the backend accepts anything within these limits.
MIN_SAMPLE_RATE = _int("MIN_SAMPLE_RATE", 225_001)
MAX_SAMPLE_RATE = _int("MAX_SAMPLE_RATE", 3_200_000)
# Manual gain range (dB). Tuner gain tables differ, but all RTL-SDR gains fall
# within this window; the UI offers a fixed subset.
MIN_GAIN_DB = _float("MIN_GAIN_DB", 0.0)
MAX_GAIN_DB = _float("MAX_GAIN_DB", 50.0)
# Hard cap on total scan wall-clock time (s). Cooperative cancellation is also
# available via Radio.cancel_scan().
SCAN_MAX_WALL_TIME = _float("SCAN_MAX_WALL_TIME", 120.0)
# rtl_tcp auto-reconnect backoff (seconds; exponential up to the max).
RECONNECT_DELAY = _float("RECONNECT_DELAY", 2)
RECONNECT_MAX_DELAY = _float("RECONNECT_MAX_DELAY", 30)

# ---- Spectrum streaming ----
SPECTRUM_BINS = _int("SPECTRUM_BINS", 2048)  # power bins sent per frame (max-pooled from FFT_SIZE)
BROADCAST_INTERVAL = _float("BROADCAST_INTERVAL", 0.02)  # ~50 fps cap

# ---- Ollama ----
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:latest")
OLLAMA_TIMEOUT = _float("OLLAMA_TIMEOUT", 180)
MAX_HISTORY_MESSAGES = _int("MAX_HISTORY_MESSAGES", 40)  # context-window safety
MAX_SIGNALS = _int("MAX_SIGNALS", 10)  # signals returned to the model per tool call
METRICS_INTERVAL = _int("METRICS_INTERVAL", 30)  # recompute signal metrics every N frames
