"""FastAPI app: REST + WebSocket bridge between browser, radio, and Ollama."""
import asyncio
import json
import logging
import queue
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import demod
from ai_agent import chat as run_ai_chat, chat_stream as run_ai_chat_stream
from radio import Radio

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    tasks = [
        asyncio.create_task(_broadcast_loop()),
        asyncio.create_task(_audio_broadcast_loop()),
    ]
    if config.AUTO_CONNECT:
        try:
            radio.connect()
        except Exception as e:  # noqa: BLE001
            log.warning("auto-connect failed (will retry on demand): %s", e)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        radio.disconnect()


app = FastAPI(title="Wideband-SDR", lifespan=lifespan)
radio = Radio()

clients: set[WebSocket] = set()
audio_clients: set[WebSocket] = set()

# Guards mutation of config.OLLAMA_URL / config.OLLAMA_MODEL from concurrent
# /api/ollama/config requests. Reads of those strings are plain atomic reference
# swaps, so they don't need the lock.
_ollama_lock = threading.Lock()


class ConnectReq(BaseModel):
    host: str | None = None
    port: int | None = None


class TuneReq(BaseModel):
    freq_hz: float


class GainReq(BaseModel):
    gain: str


class SampleRateReq(BaseModel):
    sample_rate: int


class FreqCorrectionReq(BaseModel):
    ppm: int


class ChatReq(BaseModel):
    message: str
    history: list[dict] = []
    model: str | None = None


class OllamaConfigReq(BaseModel):
    url: str | None = None
    model: str | None = None


async def _broadcast_loop():
    last = -1
    cached_payload = None
    while True:
        if radio.latest_spectrum is not None and radio.seq != last:
            last = radio.seq
            # Serialize once, send to all clients. Caching avoids re-serializing
            # the same dict if multiple broadcast iterations see the same seq.
            cached_payload = json.dumps(radio.latest_spectrum)
            for ws in list(clients):
                try:
                    await ws.send_text(cached_payload)
                except Exception:  # noqa: BLE001
                    clients.discard(ws)
        await asyncio.sleep(config.BROADCAST_INTERVAL)


async def _audio_broadcast_loop():
    # Pre-allocate: convert numpy → bytes is cheap but doing it per-client
    # wastes CPU when multiple audio clients are connected. Convert once,
    # send the same bytes to all clients.
    while True:
        if audio_clients:
            while True:
                try:
                    audio = radio.audio_queue.get_nowait()
                except queue.Empty:
                    break
                data = audio.astype("<f4").tobytes()
                dead = []
                for ws in list(audio_clients):
                    try:
                        await ws.send_bytes(data)
                    except Exception:  # noqa: BLE001
                        dead.append(ws)
                for ws in dead:
                    audio_clients.discard(ws)
        # Shorter sleep when there are audio clients to reduce latency;
        # the queue drain is non-blocking so we just poll frequently.
        await asyncio.sleep(0.005 if audio_clients else 0.02)


# ---- REST API ----
@app.get("/api/status")
def api_status():
    return radio.status()


@app.post("/api/connect")
def api_connect(req: ConnectReq):
    try:
        radio.connect(req.host, req.port)
        return radio.status()
    except Exception as e:  # noqa: BLE001
        return {"error": f"connect failed: {e}"}


@app.post("/api/disconnect")
def api_disconnect():
    radio.disconnect()
    return {"ok": True}


@app.post("/api/tune")
def api_tune(req: TuneReq):
    return radio.tune(req.freq_hz)


@app.post("/api/gain")
def api_gain(req: GainReq):
    g = req.gain.strip()
    if g.lower() == "auto":
        return radio.set_gain("auto")
    try:
        return radio.set_gain(float(g))
    except ValueError:
        return {"error": f"invalid gain: {req.gain!r}"}


@app.post("/api/sample_rate")
def api_sample_rate(req: SampleRateReq):
    return radio.set_sample_rate(req.sample_rate)


@app.post("/api/freq_correction")
def api_freq_correction(req: FreqCorrectionReq):
    return radio.set_freq_correction(req.ppm)


@app.post("/api/scan/cancel")
def api_scan_cancel():
    radio.cancel_scan()
    return {"ok": True}


@app.post("/api/chat")
def api_chat(req: ChatReq):
    return run_ai_chat(req.message, req.history, radio, model=req.model)


@app.post("/api/chat/stream")
def api_chat_stream(req: ChatReq):
    return StreamingResponse(
        run_ai_chat_stream(req.message, req.history, radio, model=req.model),
        media_type="application/x-ndjson",
    )


def _ollama_status():
    url = config.OLLAMA_URL
    try:
        resp = requests.get(f"{url}/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m.get("name") for m in resp.json().get("models", [])]
        return {"connected": True, "url": url, "model": config.OLLAMA_MODEL, "models": models}
    except Exception as e:  # noqa: BLE001
        return {"connected": False, "url": url, "model": config.OLLAMA_MODEL, "error": str(e)}


@app.get("/api/ollama/status")
def api_ollama_status():
    return _ollama_status()


@app.post("/api/ollama/config")
def api_ollama_config(req: OllamaConfigReq):
    with _ollama_lock:
        if req.url:
            config.OLLAMA_URL = req.url.strip().rstrip("/")
        if req.model:
            config.OLLAMA_MODEL = req.model.strip()
    return _ollama_status()


@app.get("/api/models")
def api_models():
    status = _ollama_status()
    out = {"models": status.get("models", [])}
    if status.get("error"):
        out["error"] = status["error"]
    return out


# ---- WebSocket ----
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            text = await ws.receive_text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            t = data.get("type")
            if t == "tune":
                radio.tune(float(data.get("freq_hz", 0)))
            elif t == "gain":
                g = str(data.get("gain", "auto"))
                radio.set_gain("auto" if g.lower() == "auto" else float(g))
            elif t == "sample_rate":
                radio.set_sample_rate(int(data.get("sample_rate", config.SAMPLE_RATE)))
            elif t == "freq_correction":
                radio.set_freq_correction(int(data.get("ppm", 0)))
            elif t == "config":
                radio.set_audio_mode(data.get("mode", config.DEFAULT_DEMOD))
                if data.get("bandwidth"):
                    radio.set_audio_bandwidth(int(data["bandwidth"]))
                if data.get("deemphasis"):
                    radio.set_audio_deemphasis(data["deemphasis"])
                if data.get("tone") is not None:
                    radio.set_audio_tone(float(data["tone"]))
                if data.get("bfo") is not None:
                    radio.set_audio_bfo(float(data["bfo"]))
                if data.get("nr") is not None:
                    radio.set_audio_nr(float(data["nr"]))
                if data.get("squelch") is not None:
                    radio.set_audio_squelch(float(data["squelch"]))
                radio.commit_audio_config()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    await ws.accept()
    audio_clients.add(ws)
    radio.set_audio_enabled(True)
    try:
        await ws.send_text(json.dumps({
            "type": "info", "audio_rate": demod.AUDIO_RATE, "channels": 1,
        }))
        while True:
            text = await ws.receive_text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            t = data.get("type")
            if t == "mode":
                radio.set_audio_mode(data.get("mode", config.DEFAULT_DEMOD))
                radio.commit_audio_config()
            elif t == "config":
                radio.set_audio_mode(data.get("mode", config.DEFAULT_DEMOD))
                if data.get("bandwidth"):
                    radio.set_audio_bandwidth(int(data["bandwidth"]))
                if data.get("deemphasis"):
                    radio.set_audio_deemphasis(data["deemphasis"])
                if data.get("tone") is not None:
                    radio.set_audio_tone(float(data["tone"]))
                if data.get("bfo") is not None:
                    radio.set_audio_bfo(float(data["bfo"]))
                if data.get("nr") is not None:
                    radio.set_audio_nr(float(data["nr"]))
                if data.get("squelch") is not None:
                    radio.set_audio_squelch(float(data["squelch"]))
                radio.commit_audio_config()
            elif t == "bandwidth":
                if data.get("bandwidth"):
                    radio.set_audio_bandwidth(int(data["bandwidth"]))
                    radio.commit_audio_config()
    except WebSocketDisconnect:
        pass
    finally:
        audio_clients.discard(ws)
        if not audio_clients:
            radio.set_audio_enabled(False)


# Static frontend (mounted last so /api and /ws take precedence).
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
