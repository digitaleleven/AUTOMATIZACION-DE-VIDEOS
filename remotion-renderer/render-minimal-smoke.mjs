import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition } from "@remotion/renderer";
import { copyFile, mkdir, mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGE_DIR = path.dirname(fileURLToPath(import.meta.url));
const CHROMIUM = path.join(PACKAGE_DIR, "tools/chromium-offline.sh");

const args = process.argv.slice(2);
const valueAfter = (name) => {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : undefined;
};

const inputPath = valueAfter("--input");
const outputPath = valueAfter("--output");
if (!inputPath || !outputPath) {
  throw new Error(
    "Usage: node render-minimal-smoke.mjs --input <validated-mp4> --output <new-mp4>",
  );
}

const workDirectory = await mkdtemp(
  path.join(os.tmpdir(), "matrix-motion-minimal-smoke-"),
);
const publicDirectory = path.join(workDirectory, "public");
await mkdir(publicDirectory, { recursive: true });
await copyFile(
  inputPath,
  path.join(publicDirectory, "optimized-source-3s.mp4"),
);

try {
  const serveUrl = await bundle({
    entryPoint: path.join(PACKAGE_DIR, "src/minimal-smoke-entry.tsx"),
    publicDir: publicDirectory,
    onProgress: (progress) => console.log(`bundle_progress=${progress}%`),
  });
  const chromiumOptions = { enableMultiProcessOnLinux: true };
  const composition = await selectComposition({
    serveUrl,
    id: "MATRIX-MOTION-Minimal-Smoke",
    browserExecutable: CHROMIUM,
    chromiumOptions,
  });

  if (
    composition.width !== 1080 ||
    composition.height !== 1920 ||
    composition.fps !== 25 ||
    composition.durationInFrames !== 75
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

  await renderMedia({
    composition,
    serveUrl,
    codec: "h264",
    outputLocation: outputPath,
    browserExecutable: CHROMIUM,
    chromiumOptions,
    concurrency: 1,
    maxRetries: 0,
    crf: 22,
    pixelFormat: "yuv420p",
    audioCodec: "aac",
    licenseKey: null,
    logLevel: "info",
  });
} finally {
  await rm(workDirectory, { recursive: true, force: true });
}
