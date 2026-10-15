// AudioWorklet processor: buffers incoming PCM chunks (posted from the main
// thread over the port) and plays them back continuously on the audio thread.
// A small pre-roll buffer absorbs network/backend jitter so playback stays smooth.
//
// Runs on the dedicated audio thread, so it is immune to main-thread stalls
// (unlike ScriptProcessor, which competed with canvas rendering).
class SdrAudioProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunks = [];   // queue of Float32Array PCM chunks
    this.buffered = 0;  // total queued samples
    this.started = false;
    this.port.onmessage = (e) => {
      const chunk = e.data;
      this.chunks.push(chunk);
      this.buffered += chunk.length;
      // Cap the buffer (~1 s) so a suspended tab can't grow memory unboundedly.
      // If the buffer overflows (backend faster than realtime), drop the oldest
      // chunk to re-sync rather than letting latency grow without bound.
      while (this.buffered > 48000 && this.chunks.length) {
        this.buffered -= this.chunks[0].length;
        this.chunks.shift();
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (!out) return true;
    const n = out.length;

    // Pre-roll: wait until ~250 ms is buffered before starting, so jitter can't
    // starve the audio thread and cause dropouts. With the multi-process backend
    // the jitter source is mostly gone, but a healthy buffer absorbs network
    // latency spikes, GC pauses, and any residual scheduling jitter.
    if (!this.started) {
      if (this.buffered < 12000) {  // 250 ms @ 48 kHz
        out.fill(0);
        return true;
      }
      this.started = true;
    }

    let written = 0;
    while (written < n && this.chunks.length) {
      const chunk = this.chunks[0];
      const take = Math.min(chunk.length, n - written);
      out.set(chunk.subarray(0, take), written);
      written += take;
      if (take < chunk.length) {
        this.chunks[0] = chunk.subarray(take);
      } else {
        this.chunks.shift();
      }
      this.buffered -= take;
    }
    if (written < n) {
      out.fill(0, written); // underrun: silence the remainder
    }
    return true;
  }
}

registerProcessor("sdr-audio-processor", SdrAudioProcessor);
