# Suggested next features

Ideas gathered from the existing codebase, README limitations, and the current
`feature/refactoring` work. Grouped by theme, roughly in priority order within
each section.

## 1. Finish scan/stream cancellation (follow-up)

The refactoring made scans cooperatively cancellable via `cancel_scan()` +
`SCAN_MAX_WALL_TIME`, but a client disconnect mid-`/api/chat/stream` is not yet
detected because the generator blocks inside `radio.scan` on a threadpool thread.

- Run tool execution in a separate worker thread with a `Future`, and have the
  streaming generator poll/cancel it so a dropped client aborts an in-flight scan
  promptly.
- Expose a "Stop" affordance in the chat UI that calls `POST /api/scan/cancel`.

## 2. Real signal classification

The README already flags that modulation hints are bandwidth-only heuristics.

- Replace `guess_modulation` with a spectral-shape classifier (bandwidth +
  sideband symmetry + presence/absence of a carrier) to reliably tell
  CW / SSB / AM / NBFM / WFM / narrow-digital apart.
- Optionally add cyclostationary analysis for the hard cases (PSK vs. FSK).
- Feed the result to both the inspector and the AI assistant, which would make
  `optimize`/`characterize` meaningfully better.

## 3. Server-side persistence

Favourites and UI settings live in `localStorage` (per-browser) and Ollama config
is in-memory (lost on restart).

- Persist favourites + Ollama config (and optionally UI settings) on the backend
  (a small SQLite or JSON file), so multiple browsers share state and restarts
  keep it.
- Add REST endpoints for favourites instead of the client-only list today.

## 4. Broadcast-FM extras (stereo + RDS)

A flagship differentiator for the "web radio" positioning in the 88–108 MHz band.

- Stereo FM decoding (pilot tone + L−R subcarrier) and mono/stereo toggle.
- RDS decoding (station name, "now playing", PI code) surfaced in the UI.

## 5. Headless / scheduled recording

Recording is currently client-side only (`MediaRecorder` → WAV download).

- Server-side recording (the backend already has the demodulated audio queue), so
  recordings don't require a browser to stay open.
- Scheduled/timer recordings (e.g. capture a specific station at a set time) and
  a recordings browser/list endpoint.

## 6. Multi-client coherence

The backend is positioned as a single-client-to-rtl_tcp mux for many browsers,
but UI state (favourites, settings) isn't shared.

- Broadcast radio state changes (tune/gain/demod) to all connected clients so
  every browser reflects the same live receiver, not just the spectrum.

## 7. Spectrum transport efficiency (perf)

At `FFT_SIZE=8192` × ~30 fps the JSON spectrum is ~1.2 MB/s per browser.

- Downsample `power` for transport (it's rendered into a canvas far narrower than
  8192 px anyway), or
- Send the spectrum as a `float32` `ArrayBuffer` over the WebSocket instead of
  JSON text. Biggest scaling win for many simultaneous clients.

## 8. Security (optional, for non-LAN exposure)

Currently any device on the LAN can control the radio and drive Ollama.

- Optional token auth (`Authorization` header / query param) gating `/api/*`,
  `/ws`, and `/ws/audio`.
- A simple rate limit on `/api/chat` to bound Ollama load/cost.

## 9. DX / observability

- Surface the dongle's actual tuner type + gain table (the rtl_tcp banner already
  reports tuner/gain count) instead of the hardcoded gain dropdown.
- Structured logs / a `/healthz` endpoint and a basic metrics view (CPU, dropped
  samples, reconnect count).
- Add `ruff` + `mypy` to CI, and `pytest-cov` coverage reporting (frontend logic
  in `app.js` is currently untested).

## 10. Test coverage

- `main.py` (REST + WebSocket) has zero tests; add them with a FastAPI test
  client.
- Add an integration test against a mock rtl_tcp server that streams known I/Q to
  exercise the full `Radio._reader_loop` → DSP → broadcast path, reconnect
  backoff, and `scan`/`characterize`.
- Extract testable helpers from `frontend/app.js` (frequency formatting, zoom/snap
  math, persistence serialization) so the 1 780-line file gets coverage.
