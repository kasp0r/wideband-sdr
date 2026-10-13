import numpy as np
from numpy.fft import rfft, rfftfreq

from demod import Demodulator, _deemphasize

FS = 2_400_000
AUDIO = 48000


def dominant_tone(audio, fs=AUDIO):
    sp = np.abs(rfft(audio))
    fr = rfftfreq(len(audio), 1 / fs)
    return float(fr[np.argmax(sp)])


def make_t(fs, seconds=0.1):
    return np.arange(int(fs * seconds)) / fs


def test_am_tone():
    t = make_t(FS)
    env = 1.0 * (1 + 0.5 * np.cos(2 * np.pi * 1000 * t))
    audio = Demodulator(FS, "am").process(env.astype(np.complex128), 0.0)
    assert abs(dominant_tone(audio) - 1000) < 100


def test_nbfm_tone():
    t = make_t(FS)
    dev = 5000.0
    phase = (dev / 1000.0) * np.sin(2 * np.pi * 1000 * t)
    audio = Demodulator(FS, "nbfm").process(np.exp(1j * phase).astype(np.complex128), 0.0)
    assert abs(dominant_tone(audio) - 1000) < 100


def test_wfm_tone():
    t = make_t(FS)
    dev = 75000.0
    phase = (dev / 1000.0) * np.sin(2 * np.pi * 1000 * t)
    audio = Demodulator(FS, "wfm").process(np.exp(1j * phase).astype(np.complex128), 0.0)
    assert abs(dominant_tone(audio) - 1000) < 100


def test_usb_lsb_tone():
    t = make_t(FS)
    usb = Demodulator(FS, "usb").process(np.exp(2j * np.pi * 1000 * t).astype(np.complex128), 0.0)
    lsb = Demodulator(FS, "lsb").process(np.exp(-2j * np.pi * 1000 * t).astype(np.complex128), 0.0)
    assert abs(dominant_tone(usb) - 1000) < 100
    assert abs(dominant_tone(lsb) - 1000) < 100


def test_ssb_bfo_shifts_pitch():
    # A BFO offset shifts the recovered SSB audio pitch (positive = higher), for both
    # USB and LSB: a 1 kHz tone with a +500 Hz BFO should land at 1.5 kHz.
    t = make_t(FS)
    usb = np.exp(2j * np.pi * 1000 * t).astype(np.complex128)
    lsb = np.exp(-2j * np.pi * 1000 * t).astype(np.complex128)
    assert abs(dominant_tone(Demodulator(FS, "usb", bfo=500).process(usb, 0.0)) - 1500) < 100
    assert abs(dominant_tone(Demodulator(FS, "lsb", bfo=500).process(lsb, 0.0)) - 1500) < 100
    assert abs(dominant_tone(Demodulator(FS, "usb", bfo=-500).process(usb, 0.0)) - 500) < 100


def test_bandwidth_narrows_filter():
    t = make_t(FS)
    env = 1 + 0.5 * np.cos(2 * np.pi * 1000 * t) + 0.5 * np.cos(2 * np.pi * 5000 * t)
    iq = env.astype(np.complex128)

    def tone_power(audio, f):
        sp = np.abs(rfft(audio))
        fr = rfftfreq(len(audio), 1 / AUDIO)
        return sp[np.argmin(np.abs(fr - f))]

    wide = Demodulator(FS, "am", bandwidth=16000).process(iq, 0.0)
    narrow = Demodulator(FS, "am", bandwidth=6000).process(iq, 0.0)

    p5k_wide = tone_power(wide, 5000)
    p5k_narrow = tone_power(narrow, 5000)
    # 5 kHz tone should be strongly rejected when the filter is narrowed
    assert 20 * np.log10((p5k_wide + 1e-9) / (p5k_narrow + 1e-9)) > 15


def test_deemphasis_attenuates_highs():
    fs = 48000
    t = np.arange(fs // 4) / fs
    sig = np.sin(2 * np.pi * 1000 * t) + np.sin(2 * np.pi * 8000 * t)

    def mag(x, f):
        sp = np.abs(rfft(x))
        fr = rfftfreq(len(x), 1 / fs)
        return sp[np.argmin(np.abs(fr - f))]

    out = _deemphasize(sig, fs, 75e-6)
    assert mag(out, 8000) / mag(out, 1000) < mag(sig, 8000) / mag(sig, 1000)


def test_soft_limiter_bounded():
    t = np.arange(48000 // 4) / 48000
    x = np.sin(2 * np.pi * 1000 * t) * 0.5
    x[len(x) // 2:] = 50.0  # massive overload
    d = Demodulator(FS, "am")
    out = d._normalize(x)
    assert np.all(np.abs(out) <= 1.0)
    # no hard-clip plateau: values should vary smoothly rather than saturate to a flat 1.0
    assert np.max(np.abs(out)) < 1.0


def test_tone_tilt_boosts_highs_when_positive():
    fs = 48000
    t = np.arange(fs // 4) / fs
    sig = np.sin(2 * np.pi * 300 * t) + np.sin(2 * np.pi * 4000 * t)

    def mag(x, f):
        sp = np.abs(rfft(x))
        fr = rfftfreq(len(x), 1 / fs)
        return sp[np.argmin(np.abs(fr - f))]

    flat = Demodulator(FS, "am")._tone(sig)          # tone defaults to 0
    bright = Demodulator(FS, "am", tone=1.0)._tone(sig)
    dark = Demodulator(FS, "am", tone=-1.0)._tone(sig)

    ratio = lambda x: mag(x, 4000) / mag(x, 300)
    assert ratio(bright) > ratio(flat) > ratio(dark)


def test_nr_lowpass_attenuates_hiss():
    t = make_t(FS)
    env = 1 + 0.5 * np.cos(2 * np.pi * 1000 * t) + 0.5 * np.cos(2 * np.pi * 6000 * t)
    iq = env.astype(np.complex128)

    def mag(audio, f):
        sp = np.abs(rfft(audio))
        fr = rfftfreq(len(audio), 1 / AUDIO)
        return sp[np.argmin(np.abs(fr - f))]

    plain = Demodulator(FS, "am").process(iq, 0.0)
    reduced = Demodulator(FS, "am", nr_cutoff=3000).process(iq, 0.0)
    # With noise reduction on, the 6 kHz component should drop relative to 1 kHz.
    assert mag(reduced, 6000) / mag(reduced, 1000) < mag(plain, 6000) / mag(plain, 1000)
