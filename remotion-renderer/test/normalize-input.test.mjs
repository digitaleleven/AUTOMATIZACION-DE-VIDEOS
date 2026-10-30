import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, writeFile, chmod } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const rendererRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const normalizeScript = path.join(rendererRoot, "src/normalize-input.mjs");

const probeJson = (width, height, fps, color) => JSON.stringify({
  streams: [
    {
      index: 0,
      codec_type: "video",
      codec_name: color === "hlg" ? "hevc" : "h264",
      width,
      height,
      pix_fmt: color === "hlg" ? "yuv420p10le" : "yuv420p",
      avg_frame_rate: `${fps}/1`,
      r_frame_rate: `${fps}/1`,
      color_range: color === "hlg" ? "tv" : "tv",
      color_primaries: color === "hlg" ? "bt2020" : "bt709",
      color_transfer: color === "hlg" ? "arib-std-b67" : "bt709",
      color_space: color === "hlg" ? "bt2020nc" : "bt709",
      side_data_list: [],
      tags: {},
    },
    {
      index: 1,
      codec_type: "audio",
      codec_name: "aac",
      sample_rate: "48000",
      channels: 2,
    },
  ],
  format: { duration: "59.98" },
});

async function setupFakeTools(t) {
  const dir = await mkdtemp(path.join(os.tmpdir(), "matrix-normalize-test-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const bin = path.join(dir, "bin");
  await mkdir(bin);
  const argsLog = path.join(dir, "ffmpeg-args.txt");
  const probeLog = path.join(dir, "ffprobe-inputs.txt");
  const ffprobe = `#!/bin/sh
last=""
for arg in "$@"; do last="$arg"; done
printf '%s\\n' "$last" >> "$FFPROBE_LOG"
case "$last" in
  *normalized.mp4) printf '%s' '${probeJson(2160, 3840, 30, "sdr")}' ;;
  *low-resolution.MOV) printf '%s' '${probeJson(1080, 1920, 30, "sdr")}' ;;
  *) printf '%s' '${probeJson(2160, 3840, 60, "hlg")}' ;;
esac
`;
  const ffmpeg = `#!/bin/sh
last=""
for arg in "$@"; do last="$arg"; done
printf '%s\\n' "$@" > "$FFMPEG_ARGS_LOG"
: > "$last"
`;
  await writeFile(path.join(bin, "ffprobe"), ffprobe);
  await writeFile(path.join(bin, "ffmpeg"), ffmpeg);
  await chmod(path.join(bin, "ffprobe"), 0o755);
  await chmod(path.join(bin, "ffmpeg"), 0o755);
  return { dir, bin, argsLog, probeLog };
}

test("4K normalization uses 2160x3840 at 30 fps and explicit SDR BT.709 tags", async (t) => {
  const tools = await setupFakeTools(t);
  const input = path.join(tools.dir, "source.MOV");
  const output = path.join(tools.dir, "normalized.mp4");
  await writeFile(input, "mock original");
  const result = spawnSync(
    process.execPath,
    [normalizeScript, input, output, "--master-4k-proxy"],
    {
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${tools.bin}${path.delimiter}${process.env.PATH || ""}`,
        FFMPEG_ARGS_LOG: tools.argsLog,
        FFPROBE_LOG: tools.probeLog,
      },
    },
  );
  assert.equal(
    result.status,
    0,
    `${result.stderr}\n${result.stdout}\nFFmpeg args:\n${await readFile(tools.argsLog, "utf8")}\nFFprobe inputs:\n${await readFile(tools.probeLog, "utf8")}`,
  );
  const report = JSON.parse(result.stdout);
  assert.equal(report.profile_id, "matrix-sdr-bt709-2160x3840-30-crf17-v1");
  assert.deepEqual([report.width, report.height, report.fps], [2160, 3840, 30]);
  assert.equal(report.color_primaries, "bt709");
  assert.equal(report.color_transfer, "bt709");
  assert.equal(report.color_space, "bt709");
  assert.equal(report.color_range, "tv");
  const args = await readFile(tools.argsLog, "utf8");
  assert.match(args, /fps=30/);
  assert.match(args, /scale=2160:3840/);
  assert.match(args, /-crf\n17/);
  assert.match(args, /-color_trc\nbt709/);
});

test("4K normalization rejects a smaller source before invoking FFmpeg", async (t) => {
  const tools = await setupFakeTools(t);
  const input = path.join(tools.dir, "low-resolution.MOV");
  const output = path.join(tools.dir, "normalized.mp4");
  await writeFile(input, "mock original");
  const result = spawnSync(
    process.execPath,
    [normalizeScript, input, output, "--master-4k-proxy"],
    {
      encoding: "utf8",
      env: {
        ...process.env,
        PATH: `${tools.bin}${path.delimiter}${process.env.PATH || ""}`,
        FFMPEG_ARGS_LOG: tools.argsLog,
      },
    },
  );
  assert.equal(result.status, 1);
  assert.match(result.stderr, /requiere una fuente vertical de 2160 × 3840/);
  await assert.rejects(readFile(tools.argsLog));
});
