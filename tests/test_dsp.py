import numpy as np
import pytest

import dsp


def test_to_complex_uint8_interleave():
    raw = np.array([128, 200, 127, 54], dtype=np.uint8)  # I0,Q0,I1,Q1
    iq = dsp.to_complex(raw)
    assert iq.shape == (2,)
    assert np.iscomplexobj(iq)
    assert iq[0].real == pytest.approx(0.5)    # 128 - 127.5
    assert iq[0].imag == pytest.approx(72.5)   # 200 - 127.5
    assert iq[1].real == pytest.approx(-0.5)
    assert iq[1].imag == pytest.approx(-73.5)


def test_to_complex_passthrough_complex():
    z = np.array([1 + 2j, 3 - 4j])
    out = dsp.to_complex(z)
    assert out.dtype == np.complex64
    assert out[0] == 1 + 2j


def test_compute_averaged_spectrum_shape_and_freqs():
    fs = 2_400_000
    fft_size = 256
    center = 100_000_000
    iq = (np.random.randn(fft_size * 2) + 1j * np.random.randn(fft_size * 2))
    freqs, power = dsp.compute_averaged_spectrum(iq, fs, fft_size, center, averages=2)
    assert freqs.shape == (fft_size,)
    assert power.shape == (fft_size,)
    assert freqs[0] == pytest.approx(center - fs / 2)
    assert freqs[-1] == pytest.approx(center + fs / 2 - fs / fft_size)
    # power is normalized so the peak is 0 dB
    assert power.max() == pytest.approx(0.0, abs=1e-6)


def test_max_pool_preserves_peak():
    power = np.full(8192, -50.0)
    power[4000] = 0.0  # narrow peak
    out = dsp.max_pool(power, 2048)
    assert out.shape == (2048,)
    assert out[1000] == pytest.approx(0.0, abs=1e-6)  # bin 4000 // 4 == 1000
    assert out[0] == pytest.approx(-50.0, abs=1e-6)   # noise floor preserved


def test_max_pool_passthrough_when_n_bins_large():
    power = np.array([1.0, 2.0, 3.0])
    out = dsp.max_pool(power, 10)
    assert np.array_equal(out, power)


def test_find_peaks_detects_synthetic_signal():
    n = 2048
    center = 100e6
    freqs = np.linspace(center - 1e6, center + 1e6, n)
    power = np.full(n, -60.0)
    power += 60 * np.exp(-((freqs - (center + 5000)) / 2000) ** 2)
    peaks, noise = dsp.find_peaks(freqs, power, threshold_db=10.0)
    assert len(peaks) == 1
    assert peaks[0]["freq_hz"] == pytest.approx(center + 5000, abs=2000)
    assert peaks[0]["power_db"] > -5
    assert noise == pytest.approx(-60.0, abs=1)


def test_signal_metrics_tuning_error_and_snr():
    n = 2048
    center = 100e6
    freqs = np.linspace(center - 1e6, center + 1e6, n)
    power = np.full(n, -50.0)
    power += 50 * np.exp(-((freqs - (center + 2000)) / 4000) ** 2)
    m = dsp.signal_metrics(freqs, power, center)
    assert m["signal_detected"] is True
    assert m["tuning_error_hz"] == pytest.approx(2000, abs=1200)  # FFT bin quantization
    assert m["snr_db"] > 30
    assert m["bandwidth_hz"] > 1000


def test_signal_metrics_no_signal():
    n = 512
    freqs = np.linspace(100e6 - 1e6, 100e6 + 1e6, n)
    power = np.full(n, -80.0)
    m = dsp.signal_metrics(freqs, power, 100e6)
    assert m["signal_detected"] is False


def test_guess_modulation():
    assert "narrowband" in dsp.guess_modulation(2000)
    assert "AM" in dsp.guess_modulation(40000)
    assert "FM" in dsp.guess_modulation(150000)


def test_band_context():
    assert dsp.band_context(100_000_000) == "FM broadcast"
    assert dsp.band_context(145_000_000) == "2 m amateur"
