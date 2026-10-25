import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition } from "@remotion/renderer";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import {
  access,
  mkdir,
  mkdtemp,
  rm,
  stat,
} from "node:fs/promises";
import { performance } from "node:perf_hooks";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  buildColorManagedProxyFilter,
  REMOTION_RENDER_PROFILE,
} from "./src/render-profile.mjs";

const execFileAsync = promisify(execFile);
const PACKAGE_DIR = path.dirname(fileURLToPath(import.meta.url));
const CHROMIUM = path.join(PACKAGE_DIR, "tools/chromium-offline.sh");
const COMPOSITION_ID = "MATRIX-MOTION-Quality-Q02";
const EXPECTED_DURATION_FRAMES = 861;
const args = process.argv.slice(2);

const valueAfter = (name) => {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : undefined;
};

const inputPath = valueAfter("--input");
const outputPath = valueAfter("--output");
if (!inputPath || !outputPath) {
  throw new Error(
    "Usage: node render-quality-q02.mjs --input <original.mov> --output <new.mp4>",
  );
}

const inputStat = await stat(inputPath);
if (!inputStat.isFile() || inputStat.size === 0) {
  throw new Error(`Input is not a non-empty file: ${inputPath}`);
}

const { stdout: probeOutput } = await execFileAsync("ffprobe", [
  "-v",
  "error",
  "-show_entries",
  "format=duration:stream=codec_type,width,height,avg_frame_rate,color_range,color_space,color_transfer,color_primaries",
  "-of",
  "json",
  inputPath,
]);
const probe = JSON.parse(probeOutput);
const video = probe.streams?.find((stream) => stream.codec_type === "video");
const audio = probe.streams?.find((stream) => stream.codec_type === "audio");
if (
  video?.width !== 2160 ||
  video?.height !== 3840 ||
  video?.avg_frame_rate !== "50/1" ||
  !audio ||
  Math.abs(Number(probe.format?.duration) - 18.82) > 0.05
) {
  throw new Error(
    `Unexpected source metadata: ${JSON.stringify({
      duration: probe.format?.duration,
      video,
      hasAudio: Boolean(audio),
    })}`,
  );
}
const colorManagedProxyFilter = buildColorManagedProxyFilter(video);

const absoluteOutput = path.resolve(outputPath);
await mkdir(path.dirname(absoluteOutput), { recursive: true });
try {
  await access(absoluteOutput);
  throw new Error(`Refusing to overwrite existing output: ${absoluteOutput}`);
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}

const totalStarted = performance.now();
const workDirectory = await mkdtemp(
  path.join(os.tmpdir(), "matrix-motion-quality-q02-"),
);
const publicDirectory = path.join(workDirectory, "public");
const proxyPath = path.join(publicDirectory, "q02-source-1080.mp4");
const audioPath = path.join(publicDirectory, "q02-master-audio.m4a");
await mkdir(publicDirectory, { recursive: true });

try {
  const proxyStarted = performance.now();
  try {
    await execFileAsync("ffmpeg", [
      "-hide_banner",
      "-y",
      "-threads",
      String(REMOTION_RENDER_PROFILE.offthreadVideoThreads),
      "-i",
      path.resolve(inputPath),
      "-map",
      "0:v:0",
      "-vf",
      colorManagedProxyFilter,
      "-frames:v",
      "941",
      "-an",
      "-c:v",
      "libx264",
      "-threads",
      String(REMOTION_RENDER_PROFILE.offthreadVideoThreads),
      "-preset",
      REMOTION_RENDER_PROFILE.x264Preset,
      "-crf",
      String(REMOTION_RENDER_PROFILE.proxyCrf),
      "-pix_fmt",
      REMOTION_RENDER_PROFILE.pixelFormat,
      "-color_range",
      "tv",
      "-colorspace",
      REMOTION_RENDER_PROFILE.colorSpace,
      "-color_primaries",
      REMOTION_RENDER_PROFILE.colorSpace,
      "-color_trc",
      REMOTION_RENDER_PROFILE.colorSpace,
      "-movflags",
      "+faststart",
      proxyPath,
    ]);
    await execFileAsync("ffmpeg", [
      "-hide_banner",
      "-y",
      "-i",
      path.resolve(inputPath),
      "-map",
      "0:a:0",
      "-vn",
      "-c:a",
      "copy",
      "-movflags",
      "+faststart",
      audioPath,
    ]);
  } finally {
    console.log(
      `proxy_seconds=${((performance.now() - proxyStarted) / 1000).toFixed(3)}`,
    );
  }

  const serveUrl = await bundle({
    entryPoint: path.join(PACKAGE_DIR, "src/quality-q02-entry.tsx"),
    publicDir: publicDirectory,
    onProgress: (progress) => console.log(`bundle_progress=${progress}%`),
  });
  const chromiumOptions = { enableMultiProcessOnLinux: true };
  const composition = await selectComposition({
    serveUrl,
    id: COMPOSITION_ID,
    browserExecutable: CHROMIUM,
    chromiumOptions,
  });

  if (
    composition.width !== 1080 ||
    composition.height !== 1920 ||
    composition.fps !== 50 ||
    composition.durationInFrames !== EXPECTED_DURATION_FRAMES
  ) {
    throw new Error(
      `Unexpected composition: ${JSON.stringify({
        width: composition.width,
        height: composition.height,
        fps: composition.fps,
        durationInFrames: composition.durationInFrames,
      })}`,
    );
  }

  const renderStarted = performance.now();
  try {
    await renderMedia({
      composition,
      serveUrl,
      codec: "h264",
      outputLocation: absoluteOutput,
      browserExecutable: CHROMIUM,
      chromiumOptions,
      concurrency: 1,
      maxRetries: 0,
      disallowParallelEncoding: REMOTION_RENDER_PROFILE.disallowParallelEncoding,
      offthreadVideoThreads: REMOTION_RENDER_PROFILE.offthreadVideoThreads,
      offthreadVideoCacheSizeInBytes: REMOTION_RENDER_PROFILE.offthreadVideoCacheSizeInBytes,
      crf: 17,
      x264Preset: "medium",
      pixelFormat: "yuv420p",
      colorSpace: "bt709",
      audioCodec: "aac",
      licenseKey: null,
      logLevel: "info",
    });
  } finally {
    console.log(
      `render_seconds=${((performance.now() - renderStarted) / 1000).toFixed(3)}`,
    );
  }
} finally {
  await rm(workDirectory, { recursive: true, force: true });
  console.log(
    `total_pipeline_seconds=${((performance.now() - totalStarted) / 1000).toFixed(3)}`,
  );
}
