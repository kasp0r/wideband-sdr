"""DSP helpers: FFT, peak detection, bandwidth estimation, band lookup."""
import numpy as np


def to_complex(samples):
    """Convert interleaved uint8/int8 I/Q bytes (or an already-complex array)
    into a complex64 array.

    rtl_tcp streams raw interleaved uint8 samples centered around 127.5;
    this turns them into complex float32 samples around 0. Float32 precision
    is ample for power spectra and halves the FFT's memory traffic.
    """
    arr = np.asarray(samples)
    if arr.dtype == np.uint8:
        arr = arr.astype(np.float32) - 127.5
        return arr[0::2] + 1j * arr[1::2]
    if arr.dtype == np.int8:
        arr = arr.astype(np.float32)
        return arr[0::2] + 1j * arr[1::2]
    if arr.dtype == np.complex64:
        return arr  # already the right dtype: no copy
    return arr.astype(np.complex64)


_HANN_CACHE = {}  # fft_size -> float32 Hann window
_FREQ_CACHE = {}  # (center_freq, sample_rate, fft_size) -> frequency axis


def _hann_window(fft_size):
    w = _HANN_CACHE.get(fft_size)
    if w is None:
        w = np.hanning(fft_size).astype(np.float32)
        _HANN_CACHE[fft_size] = w
    return w


def _freq_axis(center_freq, sample_rate, fft_size):
    # The frequency axis only changes when tuning or the sample rate changes,
    # so cache it rather than recomputing (and fftshift-ing) it every frame.
    key = (int(center_freq), int(sample_rate), int(fft_size))
    freqs = _FREQ_CACHE.get(key)
    if freqs is None:
        freqs = center_freq + np.fft.fftshift(np.fft.fftfreq(fft_size, 1.0 / sample_rate))
        _FREQ_CACHE[key] = freqs
    return freqs


def compute_averaged_spectrum(iq_block, sample_rate, fft_size, center_freq, averages):
    """Average `averages` windowed FFT frames -> (freqs, power_db normalized to 0 dB peak)."""
    n = fft_size * averages
    iq = to_complex(iq_block[:n])
    if iq.size < n:
        raise ValueError("not enough samples")
    iq = iq.reshape(averages, fft_size)
    # float32 window keeps the FFT at single precision (iq is complex64).
    X = np.fft.fftshift(np.fft.fft(iq * _hann_window(fft_size), axis=1), axes=1)
    # Magnitude-squared from the real/imag parts directly (no sqrt in np.abs).
    power = np.mean(X.real ** 2 + X.imag ** 2, axis=0)
    power_db = 10.0 * np.log10(power + 1e-12)
    power_db -= power_db.max()  # 0 dB = strongest bin
    freqs = _freq_axis(center_freq, sample_rate, fft_size)
    return freqs, power_db


def max_pool(power, n_bins):
    """Max-pool `power` down to `n_bins` bins, preserving peaks for transport.

    The FFT yields `fft_size` bins, but the browser renders the trace into a
    canvas far narrower than that. Max-pooling (vs. averaging) keeps narrow peaks
    at full amplitude so click-to-tune still snaps to them. Trailing bins are
    dropped when `n_bins` doesn't divide the length evenly.
    """
    power = np.asarray(power, dtype=np.float64)
    n = power.size
    if n_bins >= n:
        return power
    factor = n // n_bins
    trimmed = power[:factor * n_bins]
    return trimmed.reshape(n_bins, factor).max(axis=1)


def estimate_bandwidth(power_db, freqs, peak_idx, level_db):
    """Width (Hz) of the region around peak_idx that stays above level_db."""
    n = len(power_db)
    left = peak_idx
    while left > 0 and power_db[left] > level_db:
        left -= 1
    right = peak_idx
    while right < n - 1 and power_db[right] > level_db:
        right += 1
    return max(float(freqs[right] - freqs[left]), float(freqs[1] - freqs[0]))


def find_peaks(freqs, power_db, threshold_db=10.0):
    """Return detected signals and the noise floor.

    A signal is any contiguous run above (noise_floor + threshold_db).
    Returns (peaks, noise_floor); each peak has freq_hz, power_db, bandwidth_hz.
    """
    noise_floor = float(np.median(power_db))
    thr = noise_floor + threshold_db
    above = power_db > thr
    peaks = []
    n = len(power_db)
    i = 0
    while i < n:
        if not above[i]:
            i += 1
            continue
        j = i
        while j < n and above[j]:
            j += 1
        idx = i + int(np.argmax(power_db[i:j]))
        bw = estimate_bandwidth(power_db, freqs, idx, power_db[idx] - 20.0)
        peaks.append({
            "freq_hz": float(freqs[idx]),
            "power_db": float(power_db[idx]),
            "bandwidth_hz": float(bw),
        })
        i = j
    return peaks, noise_floor


def guess_modulation(bandwidth_hz):
    """Heuristic modulation hint from occupied bandwidth."""
    if bandwidth_hz < 4_000:
        return "narrowband: CW / SSB / narrow digital (PSK31, etc.)"
    if bandwidth_hz < 15_000:
        return "SSB / AM / narrow digital (RTTY, PSK, FT8, etc.)"
    if bandwidth_hz < 80_000:
        return "AM voice or medium-rate digital"
    if bandwidth_hz < 250_000:
        return "FM (NBFM/WFM) or digital"
    return "wideband: broadcast FM, DAB, DVB-T, LTE"


def short_modulation(bandwidth_hz):
    """Short (one-line) modulation label for the compact UI panel."""
    if bandwidth_hz < 4_000:
        return "CW / SSB / narrow"
    if bandwidth_hz < 15_000:
        return "SSB / AM / narrow"
    if bandwidth_hz < 80_000:
        return "AM voice"
    if bandwidth_hz < 250_000:
        return "FM / NBFM"
    return "Wideband"


# Frequency -> likely service. Values in Hz. Covers the RTL-SDR tuning range.
BAND_PLAN = [
    (100_000, 530_000, "LF/MF: longwave broadcast, nav beacons"),
    (530_000, 1_710_000, "AM broadcast"),
    (1_710_000, 2_000_000, "160 m amateur + maritime"),
    (2_000_000, 3_500_000, "shortwave broadcast / utility"),
    (3_500_000, 4_000_000, "80 m amateur"),
    (4_000_000, 7_000_000, "shortwave broadcast / utility"),
    (7_000_000, 7_300_000, "40 m amateur"),
    (10_100_000, 10_150_000, "30 m amateur (digital)"),
    (14_000_000, 14_350_000, "20 m amateur"),
    (18_068_000, 18_168_000, "17 m amateur"),
    (21_000_000, 21_450_000, "15 m amateur"),
    (24_890_000, 24_990_000, "12 m amateur"),
    (28_000_000, 29_700_000, "10 m amateur + CB (27 MHz)"),
    (30_000_000, 54_000_000, "VHF low band / land mobile"),
    (54_000_000, 88_000_000, "VHF TV / low band"),
    (87_500_000, 108_000_000, "FM broadcast"),
    (108_000_000, 137_000_000, "airband (AM aviation)"),
    (137_000_000, 138_000_000, "NOAA weather satellites (APT)"),
    (144_000_000, 148_000_000, "2 m amateur"),
    (148_000_000, 156_000_000, "VHF land mobile"),
    (156_000_000, 162_000_000, "marine VHF"),
    (162_400_000, 162_550_000, "NOAA weather radio"),
    (162_550_000, 174_000_000, "VHF land mobile"),
    (174_000_000, 216_000_000, "VHF TV"),
    (222_000_000, 225_000_000, "1.25 m amateur"),
    (420_000_000, 450_000_000, "70 cm amateur"),
    (450_000_000, 470_000_000, "UHF land mobile"),
    (470_000_000, 698_000_000, "UHF TV"),
    (698_000_000, 960_000_000, "cellular (mostly digital, encrypted)"),
    (960_000_000, 1_215_000_000, "aeronautical (DME, transponders)"),
    (1_090_000_000, 1_090_500_000, "ADS-B (Mode-S, 1090 MHz)"),
    (1_215_000_000, 1_240_000_000, "GPS L2 / navigation"),
    (1_240_000_000, 1_300_000_000, "23 cm amateur"),
    (1_570_000_000, 1_580_000_000, "GPS L1"),
    (1_620_000_000, 1_660_000_000, "Inmarsat / satellite"),
    (1_660_000_000, 1_700_000_000, "GOES / weather satellites"),
]


def band_context(freq_hz):
    for lo, hi, label in BAND_PLAN:
        if lo <= freq_hz < hi:
            return label
    return "other / unspecified"


def _adjacent_interference(freqs, power_db, target_freq, bandwidth, noise_floor):
    """Strongest signal just outside the target's occupied bandwidth, in dB above noise."""
    df = float(freqs[1] - freqs[0])
    if bandwidth <= 0:
        bandwidth = df
    lo1, hi1 = target_freq - 2 * bandwidth, target_freq - bandwidth
    lo2, hi2 = target_freq + bandwidth, target_freq + 2 * bandwidth
    mask = ((freqs >= lo1) & (freqs <= hi1)) | ((freqs >= lo2) & (freqs <= hi2))
    if not np.any(mask):
        return 0.0
    return float(np.max(power_db[mask]) - noise_floor)


def signal_metrics(freqs, power_db, center_freq):
    """Quantitative metrics for the signal nearest the tuned (center) frequency.

    This is what lets the AI reason about tuning quality and suggest corrections
    (recenter, change bandwidth/mode/gain) rather than just listing peaks.
    """
    freqs = np.asarray(freqs, dtype=np.float64)
    power_db = np.asarray(power_db, dtype=np.float64)
    noise_floor = float(np.median(power_db))
    peaks, _ = find_peaks(freqs, power_db, threshold_db=8.0)

    result = {"center_hz": float(center_freq), "noise_floor_db": noise_floor}
    if not peaks:
        result["signal_detected"] = False
        result["note"] = "no signal above the noise floor near the tuned frequency"
        return result

    # The signal the receiver is tuned to = nearest peak to center.
    target = min(peaks, key=lambda p: abs(p["freq_hz"] - center_freq))
    bw = target["bandwidth_hz"]
    result.update({
        "signal_detected": True,
        "signal_hz": float(target["freq_hz"]),
        "tuning_error_hz": float(target["freq_hz"] - center_freq),
        "power_db": float(target["power_db"]),
        "snr_db": float(target["power_db"] - noise_floor),
        "bandwidth_hz": bw,
        "modulation_hint": guess_modulation(bw),
        "modulation": short_modulation(bw),
        "band": band_context(target["freq_hz"]),
        "num_signals_in_view": len(peaks),
        "adjacent_interference_db": _adjacent_interference(
            freqs, power_db, target["freq_hz"], bw, noise_floor
        ),
    })
    return result
