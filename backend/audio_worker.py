"""Standalone audio demodulation worker, run as a subprocess.

The main process spawns this via ``subprocess.Popen`` and streams raw IQ bytes
to its stdin; this process demodulates and writes float32 audio bytes to its
stdout. Running as a separate OS process sidesteps the GIL entirely — the
heavy WFM/AM/SSB demodulation no longer competes with the main process's
spectrum FFT and WebSocket serialization.

Protocol:
  * stdin:  raw interleaved uint8 I/Q samples (one FFT frame per read)
  * stdout: float32 mono audio at 48 kHz (little-endian)
  * argv:   sample_rate mode bandwidth deemphasis_tau tone bfo nr_cutoff

The worker accumulates IQ into ``sample_rate * 0.05`` sample chunks (matching
the main process's chunk size) before demodulating.
"""
import struct
import sys

import numpy as np

import config
import demod
import dsp

FRAME_BYTES = config.FFT_SIZE * config.AVERAGES * 2  # 16384 samples * 2 bytes


def main():
    sample_rate = int(sys.argv[1])
    mode = sys.argv[2]
    bandwidth = int(sys.argv[3]) if sys.argv[3] else None
    deemphasis_tau = float(sys.argv[4]) if len(sys.argv) > 4 else 75e-6
    tone = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0
    bfo = float(sys.argv[6]) if len(sys.argv) > 6 else 0.0
    nr_cutoff = float(sys.argv[7]) if len(sys.argv) > 7 else 0.0

    audio_chunk = int(sample_rate * 0.05)

    demod_inst = demod.Demodulator(
        sample_rate, mode, bandwidth,
        deemphasis_tau=deemphasis_tau, tone=tone, bfo=bfo, nr_cutoff=nr_cutoff,
    )

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    parts = []
    part_len = 0

    while True:
        raw = stdin.read(FRAME_BYTES)
        if len(raw) < FRAME_BYTES:
            break  # stdin closed (shutdown)

        iq = dsp.to_complex(np.frombuffer(raw, dtype=np.uint8))
        parts.append(iq)
        part_len += len(iq)

        if part_len >= audio_chunk:
            chunk = np.concatenate(parts)
            parts = []
            part_len = 0

            audio = demod_inst.process(chunk, 0.0)
            if len(audio):
                data = audio.astype("<f4").tobytes()
                # Length-prefixed frame: [uint32 LE length][float32 audio]
                stdout.write(struct.pack("<I", len(data)))
                stdout.write(data)
                stdout.flush()


if __name__ == "__main__":
    main()
