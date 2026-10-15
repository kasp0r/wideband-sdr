import socket
import struct
import threading
import time

import config
from radio import Radio


def _fake_radio(sample_rate=2_400_000, center_freq=100_000_000):
    """Build a Radio whose SDR I/O is stubbed so scan() can run without a dongle."""
    r = Radio()
    r.connected = True
    r.sample_rate = sample_rate
    r.center_freq = center_freq
    r.tuned = []
    r.tune = lambda f: r.tuned.append(int(f))
    r.spectrum_snapshot = lambda **kw: {"peaks": []}
    return r


def test_stale_reader_loop_exits_immediately():
    # A reader loop whose run id no longer matches must bail out without touching the
    # SDR. Otherwise a stale thread revived by connect() would double-process audio.
    r = Radio()
    r.running = True
    r._run_id = 5
    r.sdr = object()
    t0 = time.time()
    r._reader_loop(0)
    assert time.time() - t0 < 1.0


def test_disconnect_bumps_run_id():
    r = Radio()
    before = r._run_id
    r.disconnect()
    assert r._run_id > before


def test_scan_rejects_negative_step():
    r = _fake_radio()
    res = r.scan(100_000_000, 100_010_000, step_hz=-1000)
    # negative step is clamped to the min step, so the sweep still terminates
    assert res["step_hz"] >= 1_000
    assert res["step_hz"] == int(r.sample_rate * 0.5)
    assert res["restored_center_hz"] == 100_000_000
    assert r.tuned[-1] == 100_000_000  # tuning restored


def test_scan_floors_tiny_step():
    r = _fake_radio()
    res = r.scan(100_000_000, 100_050_000, step_hz=100)
    # 100 Hz step would be ~500k iterations; it must be floored to half the span
    assert res["step_hz"] == int(r.sample_rate * 0.5)
    assert len(r.tuned) <= 2  # start + restore (span covers the whole range in one step)


def test_scan_truncates_at_max_steps():
    r = _fake_radio(sample_rate=1_000)
    res = r.scan(0, 10_000, step_hz=1_000, max_steps=3)
    assert res["truncated"] is True
    assert len(r.tuned) == 3 + 1  # 3 sweep steps + 1 restore
    assert r.tuned[-1] == 100_000_000  # still restored


def test_scan_normalizes_reversed_range():
    r = _fake_radio()
    res = r.scan(100_050_000, 100_000_000)
    assert res["scanned_hz"] == [100_000_000, 100_050_000]


class _StubSdr:
    """Minimal stand-in for RtlTcpClient so control methods can run without a dongle."""

    def __init__(self):
        self.calls = []

    def set_center_freq(self, f):
        self.calls.append(("freq", int(f)))

    def set_sample_rate(self, r):
        self.calls.append(("rate", int(r)))

    def set_gain_mode(self, m):
        self.calls.append(("gain_mode", m))

    def set_gain(self, g):
        self.calls.append(("gain", g))

    def set_freq_correction(self, p):
        self.calls.append(("ppm", int(p)))


def _radio_with_stub():
    r = Radio()
    r.sdr = _StubSdr()
    r.connected = True
    return r


def test_set_sample_rate_rejects_out_of_range():
    r = _radio_with_stub()
    before = r.sample_rate
    assert "error" in r.set_sample_rate(1_000)      # below hardware min
    assert "error" in r.set_sample_rate(99_000_000)  # above hardware max
    assert r.sample_rate == before  # unchanged on rejection
    assert "error" not in r.set_sample_rate(2_400_000)


def test_set_gain_rejects_invalid():
    r = _radio_with_stub()
    assert "error" in r.set_gain(-10)
    assert "error" in r.set_gain(999)
    assert "error" in r.set_gain("banana")

    res = r.set_gain(19.7)
    assert "error" not in res
    assert r.gain == 19.7

    res = r.set_gain("auto")
    assert "error" not in res
    assert r.gain == "auto"


def test_scan_honours_cancellation():
    r = _fake_radio(sample_rate=1_000_000)

    def cancel_later():
        time.sleep(0.03)
        r.cancel_scan()

    t = threading.Thread(target=cancel_later, daemon=True)
    t.start()
    res = r.scan(100_000_000, 200_000_000, step_hz=50_000, dwell=0.2, max_steps=1000)
    t.join()

    assert res["cancelled"] is True
    assert res["restored_center_hz"] == 100_000_000
    assert r.tuned[-1] == 100_000_000  # tuning restored even when cancelled


def test_disconnect_cancels_scan():
    r = Radio()
    r._scan_cancel.clear()
    r.disconnect()
    assert r._scan_cancel.is_set()


class ReconnectMock:
    """rtl_tcp mock that drops its *first* connection after `drop_after` seconds,
    then holds subsequent (re)connections open so the client can verify reconnect."""

    def __init__(self, drop_after=0.1):
        self.drop_after = drop_after
        self.accepted = 0
        self.stop = threading.Event()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.srv.settimeout(0.2)
        buf = bytes(range(256)) * 32
        while not self.stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.accepted += 1
            conn.sendall(b"RTL0" + struct.pack(">II", 5, 29))
            first = (self.accepted == 1)
            start = time.time()
            try:
                while not self.stop.is_set():
                    if first and time.time() - start >= self.drop_after:
                        break  # drop only the first connection
                    conn.sendall(buf)
            except OSError:
                pass
            conn.close()
        self.srv.close()

    def close(self):
        self.stop.set()


def test_auto_reconnect(monkeypatch):
    monkeypatch.setattr(config, "RECONNECT_DELAY", 0.1)
    monkeypatch.setattr(config, "RECONNECT_MAX_DELAY", 0.5)

    mock = ReconnectMock(drop_after=0.1)
    radio = Radio()
    try:
        radio.connect("127.0.0.1", mock.port)
        assert radio.connected is True

        # Wait for the first drop + a reconnect cycle to complete.
        time.sleep(0.8)

        assert mock.accepted >= 2      # the client reconnected
        assert radio.connected is True  # and is now stably connected
    finally:
        radio.disconnect()
        mock.close()
