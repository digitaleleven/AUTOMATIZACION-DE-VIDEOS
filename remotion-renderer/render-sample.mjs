import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition } from "@remotion/renderer";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import {
  copyFile,
  mkdir,
  mkdtemp,
  readdir,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";

const PACKAGE_DIR = path.dirname(fileURLToPath(import.meta.url));
const FPS = 50;
const DURATION_IN_FRAMES = 285;
const DURATION_SECONDS = DURATION_IN_FRAMES / FPS;
const MIN_SOURCE_DURATION_SECONDS = 12;
const CHROMIUM = path.join(PACKAGE_DIR, "tools/chromium-offline.sh");

const run = (command, args) =>
  new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => reject(error));
    child.on("close", (code, signal) => {
      if (code === 0) {
        resolve({ stdout, stderr });
        return;
      }
      const error = new Error(
        `${command} exited with code ${code ?? "null"}${signal ? ` (${signal})` : ""}`,
      );
      error.stdout = stdout;
      error.stderr = stderr;
      error.command = [command, ...args];
      error.exitCode = code;
      error.signal = signal;
      reject(error);
    });
  });

const hashFile = async (filePath) => {
  const digest = createHash("sha256");
  const { createReadStream } = await import("node:fs");
  for await (const chunk of createReadStream(filePath)) digest.update(chunk);
  return digest.digest("hex");
};

const parseArguments = (args) => {
  const forwardedArgs = args[0] === "--" ? args.slice(1) : args;
  const parsed = {};
  for (let index = 0; index < forwardedArgs.length; index += 1) {
    const key = forwardedArgs[index];
    if (!["--input", "--output"].includes(key) || !forwardedArgs[index + 1]) {
      throw new Error("Usage: render-sample.mjs --input <video.mp4|video.mov> --output <sample.mp4>");
    }
    parsed[key.slice(2)] = forwardedArgs[++index];
  }
  if (!parsed.input || !parsed.output) {
    throw new Error("Both --input and --output are required; no source path is hard-coded.");
  }
  return parsed;
};

const parseRate = (value) => {
  const [numerator, denominator] = String(value || "0/1").split("/");
  const rate = Number(numerator) / Number(denominator);
  return Number.isFinite(rate) && rate > 0 ? rate : 0;
};

const probe = async (filePath) => {
  const { stdout } = await run("ffprobe", [
    "-v",
    "error",
    "-show_entries",
    "format=duration,size,format_name:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,sample_rate,channels",
    "-of",
    "json",
    filePath,
  ]);
  return JSON.parse(stdout);
};

const directoryBytes = async (directory) => {
  let bytes = 0;
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) bytes += await directoryBytes(entryPath);
    else if (entry.isFile()) bytes += (await stat(entryPath)).size;
  }
  return bytes;
};

const formatError = (error) => {
  const parts = [error?.stack || String(error)];
  if (error?.command) parts.push(`Command: ${error.command.join(" ")}`);
  if (error?.stdout) parts.push(`stdout:\n${error.stdout}`);
  if (error?.stderr) parts.push(`stderr:\n${error.stderr}`);
  if (error?.cause) parts.push(`Cause:\n${formatError(error.cause)}`);
  return parts.join("\n\n");
};

let outputPath;
let outputDirectory;
let workDirectory;
let succeeded = false;

try {
  const args = parseArguments(process.argv.slice(2));
  const inputPath = path.resolve(args.input);
  outputPath = path.resolve(args.output);
  outputDirectory = path.dirname(outputPath);
  await mkdir(outputDirectory, { recursive: true });

  if (path.extname(inputPath).toLowerCase() !== ".mov" &&
      path.extname(inputPath).toLowerCase() !== ".mp4") {
    throw new Error("Input must be an MP4 or MOV file.");
  }
  if (inputPath === outputPath) {
    throw new Error("The output path must not overwrite the source video.");
  }
  try {
    await stat(outputPath);
    throw new Error(`Refusing to overwrite existing output: ${outputPath}`);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  const inputStat = await stat(inputPath);
  if (!inputStat.isFile() || inputStat.size === 0) {
    throw new Error(`Input is not a non-empty regular file: ${inputPath}`);
  }
  const inputHashBefore = await hashFile(inputPath);
  const inputProbe = await probe(inputPath);
  const inputVideo = inputProbe.streams?.find((stream) => stream.codec_type === "video");
  const inputAudio = inputProbe.streams?.find((stream) => stream.codec_type === "audio");
  const inputDuration = Number(inputProbe.format?.duration);
  const sourceFps = parseRate(inputVideo?.avg_frame_rate || inputVideo?.r_frame_rate);
  if (
    !inputVideo ||
    !Number.isFinite(inputDuration) ||
    inputDuration < MIN_SOURCE_DURATION_SECONDS
  ) {
    throw new Error(
      `Input must contain a video stream of at least ${MIN_SOURCE_DURATION_SECONDS} seconds for the selected source segments.`,
    );
  }
  if (!sourceFps) throw new Error("FFprobe did not return a valid source frame rate.");

  const sourceFrameCount = Math.round(inputDuration * sourceFps);
  const sourceFile = `source${path.extname(inputPath).toLowerCase()}`;
  const audioFile = inputAudio ? "source-audio.wav" : null;

  console.log(`input=${inputPath}`);
  console.log(`input_sha256=${inputHashBefore}`);
  console.log(`input_probe=${JSON.stringify(inputProbe)}`);
  console.log(`composition=${1080}x${1920} ${FPS}fps ${DURATION_IN_FRAMES} frames`);

  workDirectory = await mkdtemp(path.join(outputDirectory, ".remotion-work-"));
  const publicDirectory = path.join(workDirectory, "public");
  await mkdir(publicDirectory, { recursive: true });
  const copiedInput = path.join(publicDirectory, sourceFile);
  await copyFile(inputPath, copiedInput);
  if ((await hashFile(copiedInput)) !== inputHashBefore) {
    throw new Error("The temporary source copy does not match the original.");
  }
  if (audioFile) {
    const copiedAudio = path.join(publicDirectory, audioFile);
    await run("ffmpeg", [
      "-nostdin",
      "-v",
      "error",
      "-i",
      inputPath,
      "-map",
      "0:a:0",
      "-vn",
      "-c:a",
      "pcm_s16le",
      copiedAudio,
    ]);
    const extractedAudioProbe = await probe(copiedAudio);
    const extractedAudio = extractedAudioProbe.streams?.find(
      (stream) => stream.codec_type === "audio",
    );
    if (
      extractedAudio?.codec_name !== "pcm_s16le" ||
      extractedAudio.sample_rate !== inputAudio.sample_rate ||
      extractedAudio.channels !== inputAudio.channels
    ) {
      throw new Error(
        `Extracted audio does not preserve source format: ${JSON.stringify({
          source: {
            sample_rate: inputAudio.sample_rate,
            channels: inputAudio.channels,
          },
          extracted: extractedAudio,
        })}`,
      );
    }
    console.log(`extracted_audio_probe=${JSON.stringify(extractedAudioProbe)}`);
  }

  const serveUrl = await bundle({
    entryPoint: path.join(PACKAGE_DIR, "src/index.tsx"),
    publicDir: publicDirectory,
    onProgress: (progress) => console.log(`bundle_progress=${progress}%`),
  });
  const inputProps = {
    sourceFile,
    sourceDurationInFrames: sourceFrameCount,
    audioFile,
  };
  const browserOptions = { enableMultiProcessOnLinux: true };
  const composition = await selectComposition({
    serveUrl,
    id: "MATRIX-MOTION-Remotion-Phase1",
    inputProps,
    browserExecutable: CHROMIUM,
    chromiumOptions: browserOptions,
  });
  if (
    composition.width !== 1080 ||
    composition.height !== 1920 ||
    composition.fps !== FPS ||
    composition.durationInFrames !== DURATION_IN_FRAMES
  ) {
    throw new Error(`Unexpected composition metadata: ${JSON.stringify({
      width: composition.width,
      height: composition.height,
      fps: composition.fps,
      durationInFrames: composition.durationInFrames,
    })}`);
  }

  const renderStart = performance.now();
  await renderMedia({
    composition,
    serveUrl,
    inputProps,
    codec: "h264",
    outputLocation: outputPath,
    browserExecutable: CHROMIUM,
    chromiumOptions: browserOptions,
    concurrency: 1,
    maxRetries: 0,
    crf: 18,
    pixelFormat: "yuv420p",
    audioCodec: inputAudio ? "aac" : undefined,
    licenseKey: null,
    logLevel: "info",
  });
  const renderSeconds = (performance.now() - renderStart) / 1000;

  const outputStat = await stat(outputPath);
  if (outputStat.size === 0) throw new Error("Remotion created an empty output.");
  const outputProbe = await probe(outputPath);
  const outputVideo = outputProbe.streams?.find((stream) => stream.codec_type === "video");
  const outputAudio = outputProbe.streams?.find((stream) => stream.codec_type === "audio");
  const outputDuration = Number(outputProbe.format?.duration);
  const outputFps = parseRate(outputVideo?.avg_frame_rate || outputVideo?.r_frame_rate);
  const validation = {
    duration: Number.isFinite(outputDuration) && Math.abs(outputDuration - DURATION_SECONDS) <= 0.06,
    dimensions: outputVideo?.width === 1080 && outputVideo?.height === 1920,
    fps: Math.abs(outputFps - FPS) < 0.01,
    videoCodec: outputVideo?.codec_name === "h264",
    audioCodec: inputAudio ? outputAudio?.codec_name === "aac" : !outputAudio,
  };
  if (Object.values(validation).some((valid) => !valid)) {
    throw new Error(`FFprobe validation failed: ${JSON.stringify({
      validation,
      outputProbe,
    })}`);
  }
  const decodeCheck = await run("ffmpeg", [
    "-v",
    "error",
    "-i",
    outputPath,
    "-f",
    "null",
    "-",
  ]);
  if (decodeCheck.stderr.trim()) console.log(`ffmpeg_decode_warnings=${decodeCheck.stderr.trim()}`);

  const inputHashAfter = await hashFile(inputPath);
  if (inputHashAfter !== inputHashBefore) {
    throw new Error("The original source hash changed during processing.");
  }
  const report = {
    status: "success",
    input: { path: inputPath, sha256Before: inputHashBefore, sha256After: inputHashAfter },
    output: { path: outputPath, sizeBytes: outputStat.size, probe: outputProbe },
    validation,
    renderSeconds,
    sourceSizeBytes: inputStat.size,
    temporaryDirectoryBytesBeforeCleanup: await directoryBytes(outputDirectory),
    nodeProcess: {
      cpuUsageMicroseconds: process.cpuUsage(),
      rssBytes: process.memoryUsage().rss,
    },
  };
  await writeFile(
    path.join(outputDirectory, "report.json"),
    `${JSON.stringify(report, null, 2)}\n`,
    { flag: "wx" },
  );
  console.log(`render_seconds=${renderSeconds.toFixed(2)}`);
  console.log(`output=${outputPath}`);
  console.log(`output_probe=${JSON.stringify(outputProbe)}`);
  console.log(`source_unchanged=${inputHashBefore === inputHashAfter}`);
  console.log("ffmpeg_decode_check=passed");
  succeeded = true;
} catch (error) {
  const errorText = formatError(error);
  console.error(errorText);
  if (outputDirectory) {
    await writeFile(path.join(outputDirectory, "error.log"), `${errorText}\n`).catch(() => {});
  }
  if (outputPath) await rm(outputPath, { force: true }).catch(() => {});
  process.exitCode = 1;
} finally {
  if (workDirectory) await rm(workDirectory, { recursive: true, force: true }).catch(() => {});
}

if (succeeded) console.log("render_status=success");
