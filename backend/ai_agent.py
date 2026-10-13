"""Ollama agent: lets the model call SDR tools (tune / scan / characterize / etc.)."""
import json

import requests

import config
import dsp

SYSTEM_PROMPT = """You are the operator assistant for Wideband-SDR, a software-defined radio
(RTL-SDR dongle served over rtl_tcp). You have tools to control the receiver and inspect the
RF spectrum.

Guidelines:
- Use the tools to actually observe the radio rather than guessing.
- `get_status` reports current tuning. `get_spectrum` reports the strongest signals.
- `tune` moves the receiver; `fine_tune` nudges it by a relative offset; `scan` sweeps a
  range; `characterize` analyzes one signal.
- `set_demod_mode` picks the demodulator (am/nbfm/wfm/usb/lsb); `set_bandwidth` sets the
  IF filter width in Hz; `set_gain` adjusts gain (dB or "auto"); `set_sample_rate` changes
  the SDR sample rate; `set_pitch` shifts the SSB audio pitch (BFO) for USB/LSB.
- `get_signal_metrics` returns quantitative measurements of the tuned signal (SNR, tuning
  error, bandwidth, modulation hint, adjacent interference).
- When a user asks to "find", "go to", or "tune to" a signal, use `scan` or `tune` as needed.
- When a user asks to "optimize", "improve", or "clean up" the signal, do this loop:
    1. call `get_signal_metrics`;
    2. apply corrections: `fine_tune(-tuning_error_hz)` to recenter, `set_bandwidth` to
       match the signal (narrow if adjacent_interference_db is high), `set_demod_mode` to
       match the modulation hint, `set_gain` if SNR is low or clipping is likely;
    3. call `get_signal_metrics` again to confirm the improvement;
    4. report what you changed and the before/after metrics.
- When identifying a signal, combine the measured bandwidth/frequency with the band label
  and your own knowledge of the radio spectrum to suggest likely signal types.
- Frequencies are in Hertz. Be concise and concrete; quote exact frequencies.

Reference band context (for interpretation):
""" + "\n".join(f"- {lo/1e6:.1f}–{hi/1e6:.1f} MHz: {label}" for lo, hi, label in dsp.BAND_PLAN)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": "Return current SDR status (center frequency, sample rate, gain).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tune",
            "description": "Tune the SDR center frequency.",
            "parameters": {
                "type": "object",
                "properties": {"freq_hz": {"type": "number", "description": "Frequency in Hz"}},
                "required": ["freq_hz"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_spectrum",
            "description": "Report the strongest signals currently in view (freq, power, bandwidth, hints).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan",
            "description": "Sweep a frequency range and return detected signals. Restores tuning afterwards.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_hz": {"type": "number", "description": "Start frequency in Hz"},
                    "stop_hz": {"type": "number", "description": "Stop frequency in Hz"},
                    "step_hz": {"type": "number", "description": "Optional step in Hz"},
                },
                "required": ["start_hz", "stop_hz"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "characterize",
            "description": "Analyze a signal at a specific frequency (bandwidth, power, modulation hint, band).",
            "parameters": {
                "type": "object",
                "properties": {"freq_hz": {"type": "number", "description": "Frequency in Hz"}},
                "required": ["freq_hz"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_gain",
            "description": "Set receiver gain in dB (e.g. 20) or 'auto'.",
            "parameters": {
                "type": "object",
                "properties": {"gain": {"type": "string"}},
                "required": ["gain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_demod_mode",
            "description": "Set the demodulation mode (am, nbfm, wfm, usb, lsb).",
            "parameters": {
                "type": "object",
                "properties": {"mode": {"type": "string", "enum": ["am", "nbfm", "wfm", "usb", "lsb"]}},
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_bandwidth",
            "description": "Set the IF filter bandwidth in Hz (e.g. 12500 for NBFM, 2700 for SSB).",
            "parameters": {
                "type": "object",
                "properties": {"bandwidth_hz": {"type": "number", "description": "Bandwidth in Hz"}},
                "required": ["bandwidth_hz"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_sample_rate",
            "description": "Set the SDR sample rate in Hz (e.g. 2400000, 960000, 250000).",
            "parameters": {
                "type": "object",
                "properties": {"sample_rate": {"type": "number", "description": "Sample rate in Hz"}},
                "required": ["sample_rate"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fine_tune",
            "description": "Nudge the tuned frequency by a relative offset in Hz (positive = up).",
            "parameters": {
                "type": "object",
                "properties": {"offset_hz": {"type": "number", "description": "Offset in Hz"}},
                "required": ["offset_hz"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_signal_metrics",
            "description": "Return quantitative metrics for the tuned signal (SNR, tuning error, bandwidth, modulation hint, adjacent interference).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_tone",
            "description": "Set the audio tone tilt EQ (-1.0 dark/warm to +1.0 bright).",
            "parameters": {
                "type": "object",
                "properties": {"tone": {"type": "number", "description": "Tone from -1.0 to 1.0"}},
                "required": ["tone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_deemphasis",
            "description": "Set the FM de-emphasis time constant (75us for Americas, 50us for Europe, or off).",
            "parameters": {
                "type": "object",
                "properties": {"deemphasis": {"type": "string", "enum": ["75us", "50us", "off"]}},
                "required": ["deemphasis"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_pitch",
            "description": "Set the SSB beat-frequency offset (audio pitch) in Hz, for USB/LSB modes.",
            "parameters": {
                "type": "object",
                "properties": {"bfo_hz": {"type": "number", "description": "Offset in Hz (e.g. -2000 to 2000)"}},
                "required": ["bfo_hz"],
            },
        },
    },
]


def _build_context(radio):
    """Snapshot the live radio state so the model starts already grounded."""
    lines = []
    try:
        s = radio.status()
        if s.get("connected"):
            lines.append(f"Tuned to {s['center_hz']/1e6:.3f} MHz, sample rate {s['sample_rate']/1e6:.2f} MHz, gain {s['gain']}")
            if s.get("freq_correction_ppm"):
                lines.append(f"Frequency correction {s['freq_correction_ppm']} ppm")
            de = {75e-6: "75 µs", 50e-6: "50 µs", 0.0: "off"}.get(
                radio.audio_deemphasis, f"{radio.audio_deemphasis * 1e6:.0f} µs")
            extra = f", pitch {radio.audio_bfo:+.0f} Hz" if radio.audio_mode in ("usb", "lsb") else ""
            lines.append(
                f"Demod {radio.audio_mode.upper()}, IF bandwidth {radio.effective_bandwidth()} Hz, "
                f"de-emphasis {de}, tone {radio.audio_tone:+.2f}{extra}"
            )
    except Exception:
        pass
    try:
        m = radio.get_signal_metrics()
        if m.get("signal_detected"):
            lines.append(
                f"Tuned signal: SNR {m['snr_db']:.1f} dB, tuning error {m['tuning_error_hz']:+.0f} Hz, "
                f"bandwidth {m['bandwidth_hz']/1e3:.1f} kHz, band '{m['band']}', hint: {m['modulation_hint']}"
            )
    except Exception:
        pass
    try:
        snap = radio.spectrum_snapshot(top_n=6)
        if snap.get("peaks"):
            tops = ", ".join(f"{p['freq_hz']/1e6:.3f} MHz ({p['power_db']:.0f} dB)" for p in snap["peaks"])
            lines.append(f"Strongest signals in view: {tops}")
    except Exception:
        pass
    if not lines:
        return "\n\nCurrent radio state: not connected or no data yet."
    return ("\n\nCurrent radio state (already known; don't re-query unless something changed):\n"
            + "\n".join("- " + l for l in lines))


def _build_messages(user_message, history, radio):
    messages = [{"role": "system", "content": SYSTEM_PROMPT + _build_context(radio)}]
    past = [
        {"role": h["role"], "content": h.get("content", "")}
        for h in (history or [])
        if h.get("role") in ("user", "assistant", "tool")
    ]
    # Keep only the most recent messages (defense-in-depth against context overflow;
    # the frontend also caps history, but we enforce it here too).
    if len(past) > config.MAX_HISTORY_MESSAGES:
        past = past[-config.MAX_HISTORY_MESSAGES:]
    messages.extend(past)
    messages.append({"role": "user", "content": user_message})
    return messages


_SIGNAL_FIELDS = ("freq_hz", "power_db", "bandwidth_hz", "modulation_hint", "band")


def _trim_signal(p):
    """Reduce a signal/peak dict to the fields the model needs, with compact numbers."""
    out = {}
    for k in _SIGNAL_FIELDS:
        if k in p and p[k] is not None:
            out[k] = p[k]
    if "freq_hz" in out:
        out["freq_hz"] = int(round(out["freq_hz"]))
    if "bandwidth_hz" in out:
        out["bandwidth_hz"] = int(round(out["bandwidth_hz"]))
    if "power_db" in out:
        out["power_db"] = round(out["power_db"], 1)
    return out


def _trim_signals(signals, max_n):
    return [_trim_signal(p) for p in (signals or [])[:max_n]]


def _execute_tool(name, args, radio):
    try:
        if name == "get_status":
            return radio.status()
        if name == "tune":
            return radio.tune(int(args["freq_hz"]))
        if name == "get_spectrum":
            snap = radio.spectrum_snapshot(top_n=config.MAX_SIGNALS)
            if isinstance(snap, dict) and isinstance(snap.get("peaks"), list):
                snap["peaks"] = _trim_signals(snap["peaks"], config.MAX_SIGNALS)
            return snap
        if name == "scan":
            result = radio.scan(int(args["start_hz"]), int(args["stop_hz"]), args.get("step_hz"))
            if isinstance(result, dict) and isinstance(result.get("signals"), list):
                result["signals"] = _trim_signals(result["signals"], config.MAX_SIGNALS)
            return result
        if name == "characterize":
            return radio.characterize(args.get("freq_hz"))
        if name == "set_gain":
            g = str(args.get("gain", "")).strip()
            gain = "auto" if g.lower() == "auto" else float(g)
            return radio.set_gain(gain)
        if name == "set_demod_mode":
            radio.set_audio_mode(args["mode"])
            return {"ok": True, "demod_mode": radio.audio_mode, "bandwidth_hz": radio.effective_bandwidth()}
        if name == "set_bandwidth":
            radio.set_audio_bandwidth(int(args["bandwidth_hz"]))
            return {"ok": True, "bandwidth_hz": radio.effective_bandwidth()}
        if name == "set_sample_rate":
            return radio.set_sample_rate(int(args["sample_rate"]))
        if name == "fine_tune":
            return radio.fine_tune(int(args["offset_hz"]))
        if name == "get_signal_metrics":
            return radio.get_signal_metrics()
        if name == "set_tone":
            radio.set_audio_tone(float(args["tone"]))
            return {"ok": True, "tone": radio.audio_tone}
        if name == "set_deemphasis":
            radio.set_audio_deemphasis(args["deemphasis"])
            return {"ok": True, "deemphasis": args["deemphasis"]}
        if name == "set_pitch":
            radio.set_audio_bfo(float(args["bfo_hz"]))
            return {"ok": True, "bfo_hz": radio.audio_bfo}
        return {"error": f"unknown tool: {name}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"tool {name} failed: {e}"}


def _ollama_chat(messages, model=None):
    resp = requests.post(
        f"{config.OLLAMA_URL}/api/chat",
        json={
            "model": model or config.OLLAMA_MODEL,
            "messages": messages,
            "tools": TOOLS,
            "stream": False,
            "options": {"temperature": 0.2},
        },
        timeout=config.OLLAMA_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _ollama_chat_stream(messages, model=None):
    """Yield {"type":"token","text":...} chunks and, at the end, a
    {"type":"tool_calls","tool_calls":[...]} event if the model called tools."""
    resp = requests.post(
        f"{config.OLLAMA_URL}/api/chat",
        json={
            "model": model or config.OLLAMA_MODEL,
            "messages": messages,
            "tools": TOOLS,
            "stream": True,
            "options": {"temperature": 0.2},
        },
        timeout=config.OLLAMA_TIMEOUT,
        stream=True,
    )
    resp.raise_for_status()

    tool_calls = {}  # index -> {"name": str, "arguments": str | dict}
    for raw in resp.iter_lines():
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = data.get("message", {})
        if msg.get("content"):
            yield {"type": "token", "text": msg["content"]}
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            idx = tc.get("index", 0)
            entry = tool_calls.setdefault(idx, {"name": "", "arguments": ""})
            if fn.get("name"):
                entry["name"] = fn["name"]
            arg = fn.get("arguments")
            if isinstance(arg, str):
                entry["arguments"] += arg
            elif isinstance(arg, dict):
                entry["arguments"] = arg
        if data.get("done"):
            break

    final_tool_calls = []
    for idx in sorted(tool_calls):
        entry = tool_calls[idx]
        args = entry["arguments"]
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except json.JSONDecodeError:
                args = {}
        final_tool_calls.append({"function": {"name": entry["name"], "arguments": args}})

    if final_tool_calls:
        yield {"type": "tool_calls", "tool_calls": final_tool_calls}


def chat(user_message, history, radio, model=None, max_tool_rounds=10):
    """Run one agent turn. history is a list of {role, content} dicts."""
    messages = _build_messages(user_message, history, radio)

    tools_called = []
    for _ in range(max_tool_rounds):
        try:
            data = _ollama_chat(messages, model)
        except requests.RequestException as e:
            return {"reply": f"⚠️ Could not reach Ollama at {config.OLLAMA_URL}: {e}",
                    "tools_called": tools_called}

        msg = data.get("message", {})
        messages.append({
            "role": "assistant",
            "content": msg.get("content", ""),
            "tool_calls": msg.get("tool_calls", []),
        })

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return {"reply": msg.get("content", ""), "tools_called": tools_called}

        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tools_called.append(name)
            result = _execute_tool(name, args, radio)
            messages.append({"role": "tool", "content": json.dumps(result)})

    return {
        "reply": "I reached my tool-calling limit. Here is what I used so far: "
                 + ", ".join(tools_called),
        "tools_called": tools_called,
    }


def chat_stream(user_message, history, radio, model=None, max_tool_rounds=10):
    """Stream one agent turn as newline-delimited JSON events.

    Events:
      {"type":"tool","name":...,"args":...}   when a tool is called
      {"type":"token","text":...}             a chunk of the final answer
      {"type":"done","reply":...}             completion
      {"type":"error","message":...}          an error
    """
    messages = _build_messages(user_message, history, radio)

    tools_called = []
    for _ in range(max_tool_rounds):
        content = ""
        tool_calls = None
        try:
            for chunk in _ollama_chat_stream(messages, model):
                if chunk["type"] == "token":
                    content += chunk["text"]
                    yield json.dumps({"type": "token", "text": chunk["text"]}) + "\n"
                elif chunk["type"] == "tool_calls":
                    tool_calls = chunk["tool_calls"]
        except requests.RequestException as e:
            yield json.dumps({"type": "error", "message": f"Could not reach Ollama: {e}"}) + "\n"
            return
        except Exception as e:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": f"Agent error: {e}"}) + "\n"
            return

        assistant_msg = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            yield json.dumps({"type": "done", "reply": content, "tools_called": tools_called}) + "\n"
            return

        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tools_called.append(name)
            yield json.dumps({"type": "tool", "name": name, "args": args}) + "\n"
            result = _execute_tool(name, args, radio)
            messages.append({"role": "tool", "content": json.dumps(result)})

    yield json.dumps({
        "type": "done",
        "reply": "I reached my tool-calling limit. Tools used: " + ", ".join(tools_called),
        "tools_called": tools_called,
    }) + "\n"
