# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Renamed the project to Wideband-SDR.
- Zoomable spectrum: mouse-wheel or slider zoom into any region (double-click to
  reset); `MIN_SPAN_HZ` sets the smallest zoom level.
- Theme selector (Black, Dark, Midnight, Amber, Light, Solarized, Nord, Terminal).
- Spectrum smoothing slider (temporal averaging of the trace).
- Cancellable, wall-clock-capped band scans (`POST /api/scan/cancel`; scans honour
  cancellation and a `SCAN_MAX_WALL_TIME` deadline).
- Gain / sample-rate input validation with configurable hardware limits
  (`MIN_SAMPLE_RATE`, `MAX_SAMPLE_RATE`, `MIN_GAIN_DB`, `MAX_GAIN_DB`).
- Spectrum transport downsampling (`SPECTRUM_BINS`) — the FFT output is max-pooled
  before streaming, cutting the WebSocket payload ~4× while preserving peaks for
  click-to-tune.

### Changed

- Replaced the deprecated `@app.on_event("startup")` hook with a `lifespan` handler
  that also shuts the radio and background loops down gracefully.
- Hardened the audio-demodulator hand-off in the reader thread against a concurrent
  settings-change race; documented the threading contract in `radio.py`.
- Tightened tuning/audio responsiveness: `audio_chunk` 0.1 → 0.05 s and
  `BROADCAST_INTERVAL` 0.03 → 0.02 s.

### Changed

- `FFT_SIZE` default 2048 → 8192 for finer zoom resolution.
- `AVERAGES` default 4 → 2 to reduce tuning latency.

### Fixed

- Click-to-tune snapping to a distant peak when zoomed (snap threshold now scales
  with the visible span).

## [0.2.0] - 2026-10-10

### Added

- Favorites (save / remove / tune frequencies, with demod mode + bandwidth).
- Audio recording to WAV with embedded metadata (frequency, demod, timestamp).
- Waterfall time axis (toggleable timestamps) and a Grab screenshot button.
- Connection history (recent SDR hosts) with autocomplete.
- AI assistant overhaul: live grounding, streaming responses, live tool progress,
  suggestion chips, model selector, persisted chat history, and `set_tone` /
  `set_deemphasis` tools.
- Persisted UI settings (floor, speed, scope, tone, volume, demod, bandwidth, gain,
  de-emphasis).
- Oscilloscope refresh-rate (Scope) slider; de-emphasis region labels; tone tilt EQ.

## [0.1.0] - 2026-10-09

### Added

- Live spectrum + waterfall with peak labels, click-to-tune, and a dB color legend.
- AM / NBFM / WFM / USB / LSB audio demodulation with bandwidth, de-emphasis, and tone controls.
- Ollama-powered AI assistant with tool-calling (`tune`, `scan`, `characterize`, `optimize`, etc.).
- Live signal inspector (SNR, tuning error, bandwidth, adjacent interference).
- Band-context indicator and activity log.
- Native rtl_tcp wire-protocol client (no `pyrtlsdr` dependency).
- Docker and docker-compose setup.
- Test suite covering DSP, demodulators, rtl_tcp protocol, and AI tool dispatch.

[Unreleased]: https://github.com/kasp0r/wideband-sdr/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/kasp0r/wideband-sdr/releases/tag/v0.2.0
[0.1.0]: https://github.com/kasp0r/wideband-sdr/releases/tag/v0.1.0
