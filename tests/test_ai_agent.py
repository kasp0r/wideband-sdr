import config

from ai_agent import _build_context, _build_messages, _execute_tool


class FakeRadio:
    def __init__(self):
        self.calls = []
        self.audio_mode = "nbfm"
        self.audio_bandwidth = None
        self.audio_tone = 0
        self.audio_deemphasis = 75e-6
        self.audio_bfo = 0

    def status(self):
        self.calls.append(("status",))
        return {"center_hz": 100_000_000}

    def tune(self, freq_hz):
        self.calls.append(("tune", freq_hz))
        return {"ok": True}

    def fine_tune(self, offset_hz):
        self.calls.append(("fine_tune", offset_hz))
        return {"ok": True}

    def spectrum_snapshot(self):
        self.calls.append(("spectrum_snapshot",))
        return {"peaks": []}

    def scan(self, start_hz, stop_hz, step_hz=None):
        self.calls.append(("scan", start_hz, stop_hz, step_hz))
        return {"signals": []}

    def characterize(self, freq_hz=None):
        self.calls.append(("characterize", freq_hz))
        return {"freq_hz": freq_hz}

    def set_gain(self, gain):
        self.calls.append(("set_gain", gain))
        return {"ok": True}

    def set_audio_mode(self, mode):
        self.calls.append(("set_audio_mode", mode))
        self.audio_mode = mode

    def set_audio_bandwidth(self, bandwidth):
        self.calls.append(("set_audio_bandwidth", bandwidth))
        self.audio_bandwidth = bandwidth

    def effective_bandwidth(self):
        return self.audio_bandwidth if self.audio_bandwidth is not None else 16000

    def set_audio_tone(self, tone):
        self.calls.append(("set_audio_tone", tone))
        self.audio_tone = tone

    def set_audio_deemphasis(self, deemphasis):
        self.calls.append(("set_audio_deemphasis", deemphasis))
        self.audio_deemphasis = deemphasis

    def set_audio_bfo(self, bfo):
        self.calls.append(("set_audio_bfo", bfo))
        self.audio_bfo = bfo

    def set_sample_rate(self, sample_rate):
        self.calls.append(("set_sample_rate", sample_rate))
        return {"ok": True}

    def get_signal_metrics(self):
        self.calls.append(("get_signal_metrics",))
        return {"snr_db": 20}


def test_tune_dispatch():
    r = FakeRadio()
    _execute_tool("tune", {"freq_hz": 90_000_000}, r)
    assert r.calls == [("tune", 90_000_000)]


def test_fine_tune_dispatch():
    r = FakeRadio()
    _execute_tool("fine_tune", {"offset_hz": 500}, r)
    assert r.calls == [("fine_tune", 500)]


def test_set_gain_auto_and_manual():
    r = FakeRadio()
    _execute_tool("set_gain", {"gain": "auto"}, r)
    _execute_tool("set_gain", {"gain": "20"}, r)
    assert r.calls[0] == ("set_gain", "auto")
    assert r.calls[1] == ("set_gain", 20.0)


def test_set_demod_mode_dispatch():
    r = FakeRadio()
    res = _execute_tool("set_demod_mode", {"mode": "wfm"}, r)
    assert r.calls == [("set_audio_mode", "wfm")]
    assert res["demod_mode"] == "wfm"


def test_set_bandwidth_dispatch():
    r = FakeRadio()
    res = _execute_tool("set_bandwidth", {"bandwidth_hz": 12500}, r)
    assert r.calls == [("set_audio_bandwidth", 12500)]
    assert res["bandwidth_hz"] == 12500


def test_set_sample_rate_dispatch():
    r = FakeRadio()
    _execute_tool("set_sample_rate", {"sample_rate": 250_000}, r)
    assert r.calls == [("set_sample_rate", 250_000)]


def test_get_signal_metrics_dispatch():
    r = FakeRadio()
    res = _execute_tool("get_signal_metrics", {}, r)
    assert res == {"snr_db": 20}
    assert r.calls == [("get_signal_metrics",)]


def test_scan_and_characterize_dispatch():
    r = FakeRadio()
    _execute_tool("scan", {"start_hz": 88e6, "stop_hz": 108e6}, r)
    _execute_tool("characterize", {"freq_hz": 100e6}, r)
    assert r.calls[0] == ("scan", 88_000_000, 108_000_000, None)
    assert r.calls[1] == ("characterize", 100_000_000)


def test_unknown_tool_returns_error():
    r = FakeRadio()
    res = _execute_tool("does_not_exist", {}, r)
    assert "error" in res
    assert r.calls == []


def test_set_tone_dispatch():
    r = FakeRadio()
    res = _execute_tool("set_tone", {"tone": 0.5}, r)
    assert r.calls == [("set_audio_tone", 0.5)]
    assert res["tone"] == 0.5


def test_set_deemphasis_dispatch():
    r = FakeRadio()
    res = _execute_tool("set_deemphasis", {"deemphasis": "50us"}, r)
    assert r.calls == [("set_audio_deemphasis", "50us")]
    assert res["deemphasis"] == "50us"


def test_set_pitch_dispatch():
    r = FakeRadio()
    res = _execute_tool("set_pitch", {"bfo_hz": 500}, r)
    assert r.calls == [("set_audio_bfo", 500.0)]
    assert res["bfo_hz"] == 500.0


def test_build_context_not_connected():
    r = FakeRadio()
    ctx = _build_context(r)
    assert "not connected or no data yet" in ctx


def test_build_context_includes_state():
    class RichRadio(FakeRadio):
        def status(self):
            return {"connected": True, "center_hz": 98_800_000, "sample_rate": 2_400_000, "gain": "auto"}

        def get_signal_metrics(self):
            return {
                "signal_detected": True,
                "snr_db": 12.3,
                "tuning_error_hz": 250.0,
                "bandwidth_hz": 200_000.0,
                "band": "FM broadcast",
                "modulation_hint": "FM (NBFM/WFM) or digital",
            }

        def spectrum_snapshot(self, top_n=20, threshold_db=10.0):
            return {"peaks": [{"freq_hz": 98_800_000.0, "power_db": -42.0}]}

    r = RichRadio()
    r.audio_mode = "wfm"
    ctx = _build_context(r)
    assert "98.800 MHz" in ctx
    assert "FM broadcast" in ctx
    assert "WFM" in ctx
    assert "de-emphasis" in ctx
    assert "tone" in ctx


def test_build_messages_includes_context_and_history():
    r = FakeRadio()
    msgs = _build_messages("hello", [{"role": "user", "content": "hi"}], r)
    assert msgs[0]["role"] == "system"
    assert "Current radio state" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "hi"}
    assert msgs[-1] == {"role": "user", "content": "hello"}


def test_build_messages_truncates_history():
    r = FakeRadio()
    history = [{"role": "user", "content": f"m{i}"} for i in range(80)]
    msgs = _build_messages("hello", history, r)
    assert len(msgs) == config.MAX_HISTORY_MESSAGES + 2  # system + capped history + user
    assert msgs[0]["role"] == "system"
    assert msgs[-1] == {"role": "user", "content": "hello"}
    assert msgs[1]["content"] == "m40"  # oldest 40 dropped


def test_get_spectrum_trims_and_caps_signals():
    class PeakRadio(FakeRadio):
        def spectrum_snapshot(self, top_n=20, threshold_db=10.0):
            return {"peaks": [
                {"freq_hz": 98_800_000.4, "power_db": -42.678, "bandwidth_hz": 200_000.6,
                 "modulation_hint": "FM", "band": "FM broadcast", "extra": "drop-me"}
                for _ in range(15)
            ]}

    r = PeakRadio()
    res = _execute_tool("get_spectrum", {}, r)
    assert len(res["peaks"]) == config.MAX_SIGNALS
    p = res["peaks"][0]
    assert p["freq_hz"] == 98_800_000
    assert p["power_db"] == -42.7
    assert p["bandwidth_hz"] == 200_001
    assert "extra" not in p


def test_scan_trims_and_caps_signals():
    class ScanRadio(FakeRadio):
        def scan(self, start_hz, stop_hz, step_hz=None):
            return {"signals": [
                {"freq_hz": 100_000_000.0, "power_db": -10.0, "bandwidth_hz": 100_000.0,
                 "modulation_hint": "FM", "band": "FM broadcast"}
                for _ in range(12)
            ], "num_signals": 12}

    r = ScanRadio()
    res = _execute_tool("scan", {"start_hz": 88e6, "stop_hz": 108e6}, r)
    assert len(res["signals"]) == config.MAX_SIGNALS
    assert res["signals"][0]["freq_hz"] == 100_000_000
