import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { runLocalProcess as run } from "../src/local-process.mjs";
import { buildMasterFilterComplex } from "../src/master-assembly.mjs";

test("real FFmpeg streams two cuts with exact retained frames and continuous audio", async () => {
  const work = await mkdtemp(path.join(tmpdir(), "mm-assembly-test-"));
  try {
    const source = path.join(work, "source.mkv");
    const output = path.join(work, "result.mkv");
    await run("ffmpeg", [
      "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=64x96:r=30:d=1.5",
      "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.5",
      "-c:v", "ffv1", "-threads", "1", "-c:a", "pcm_s16le", source,
    ]);
    const graph = buildMasterFilterComplex({
      clips: [
        { sourceStart: 3, frames: 9, outputStart: 0 },
        { sourceStart: 20, frames: 10, outputStart: 9 },
      ],
      sourceAudioStreamIndex: 1,
      colorFilter: "fps=30,scale=64:96,format=yuv420p",
      jumpFrames: [],
      includeSoundEffects: false,
      width: 64,
      height: 96,
    });
    await run("ffmpeg", [
      "-nostdin", "-v", "error", "-filter_complex_threads", "1",
      "-threads", "1", "-i", source,
      "-f", "lavfi", "-i", "color=c=black@0:s=64x96:r=30:d=1,format=rgba",
      "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
      "-c:v", "ffv1", "-threads", "1", "-c:a", "pcm_s16le", output,
    ]);
    const probe = JSON.parse((await run("ffprobe", [
      "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", output,
    ])).stdout);
    assert.equal(Number(probe.streams.find((stream) => stream.codec_type === "video").nb_read_frames), 19);
    assert.ok(Math.abs(Number(probe.format.duration) - 19 / 30) < 0.034);
    const decoded = await run("ffmpeg", [
      "-nostdin", "-v", "info", "-i", output, "-af", "silencedetect=n=-50dB:d=0.01",
      "-f", "null", "-",
    ]);
    assert.doesNotMatch(decoded.stderr, /silence_start/u);
  } finally {
    await rm(work, { recursive: true, force: true });
  }
});
