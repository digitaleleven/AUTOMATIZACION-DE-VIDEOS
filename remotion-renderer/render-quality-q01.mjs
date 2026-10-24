import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition } from "@remotion/renderer";
import { access, copyFile, mkdir, mkdtemp, rm, stat } from "node:fs/promises";
import { performance } from "node:perf_hooks";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGE_DIR = path.dirname(fileURLToPath(import.meta.url));
const CHROMIUM = path.join(PACKAGE_DIR, "tools/chromium-offline.sh");
const COMPOSITION_ID = "MATRIX-MOTION-Quality-Q01";

const args = process.argv.slice(2);
const valueAfter = (name) => {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : undefined;
};

const inputPath = valueAfter("--input");
const outputPath = valueAfter("--output");
if (!inputPath || !outputPath) {
  throw new Error(
    "Usage: node render-quality-q01.mjs --input <validated-mp4> --output <new-mp4>",
  );
}

const inputStat = await stat(inputPath);
if (!inputStat.isFile() || inputStat.size === 0) {
  throw new Error(`Input is not a non-empty file: ${inputPath}`);
}
await mkdir(path.dirname(outputPath), { recursive: true });
try {
  await access(outputPath);
  throw new Error(`Refusing to overwrite existing output: ${outputPath}`);
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}

const totalStarted = performance.now();
const workDirectory = await mkdtemp(
  path.join(os.tmpdir(), "matrix-motion-quality-q01-"),
);
const publicDirectory = path.join(workDirectory, "public");
await mkdir(publicDirectory, { recursive: true });
await copyFile(inputPath, path.join(publicDirectory, "q01-source-3s.mp4"));

try {
  const serveUrl = await bundle({
    entryPoint: path.join(PACKAGE_DIR, "src/quality-q01-entry.tsx"),
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
    composition.durationInFrames !== 150
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
      outputLocation: outputPath,
      browserExecutable: CHROMIUM,
      chromiumOptions,
      concurrency: 1,
      maxRetries: 0,
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
