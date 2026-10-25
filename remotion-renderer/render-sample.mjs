import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition } from "@remotion/renderer";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import {
  copyFile,
  mkdir,
  mkdtemp,
  readdir,
  readFile,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";
import {
  mapWordTimingsToOutput,
  parseWhisperWordTimings,
} from "./src/caption-timing.mjs";
import {
  DURATION_IN_FRAMES,
  FPS,
  SEGMENTS,
} from "./src/edit-timeline.mjs";
import {
  buildColorManagedProxyFilter,
  hasVisualContent,
  parseAllIntraFrameInfo,
  REMOTION_RENDER_PROFILE,
} from "./src/render-profile.mjs";

const PACKAGE_DIR = path.dirname(fileURLToPath(import.meta.url));
const DURATION_SECONDS = DURATION_IN_FRAMES / FPS;
const MIN_SOURCE_DURATION_SECONDS = 12;
const SAMPLE_SOURCE_FRAMES = Math.max(
  ...SEGMENTS.map((segment) => segment.sourceStart + segment.frames),
);
const SAMPLE_SOURCE_SECONDS = SAMPLE_SOURCE_FRAMES / FPS;
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
  const allowedKeys = ["--input", "--output", "--transcript", "--proxy", "--render-frames"];
  for (let index = 0; index < forwardedArgs.length; index += 1) {
    const key = forwardedArgs[index];
    if (!allowedKeys.includes(key) || !forwardedArgs[index + 1]) {
      throw new Error(
        "Usage: render-sample.mjs --input <original.mov> --transcript <whisper.json> --output <new.mp4> [--proxy <prepared-all-intra.mp4>] [--render-frames <count>]",
      );
    }
    parsed[key.slice(2)] = forwardedArgs[++index];
  }
  if (!parsed.input || !parsed.output || !parsed.transcript) {
    throw new Error("Input, word-timestamp transcript JSON, and output are required; no source path is hard-coded.");
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
    "format=duration,size,format_name:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,sample_rate,channels,pix_fmt,color_range,color_space,color_transfer,color_primaries",
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
let reportPath;
let errorLogPath;

try {
  const args = parseArguments(process.argv.slice(2));
  const inputPath = path.resolve(args.input);
  outputPath = path.resolve(args.output);
  const transcriptPath = path.resolve(args.transcript);
  const preparedProxyPath = args.proxy ? path.resolve(args.proxy) : null;
  const renderFrameCount = args["render-frames"] === undefined
    ? DURATION_IN_FRAMES
    : Number(args["render-frames"]);
  if (
    !Number.isInteger(renderFrameCount) ||
    renderFrameCount <= 0 ||
    renderFrameCount > DURATION_IN_FRAMES
  ) {
    throw new Error(`--render-frames must be an integer from 1 to ${DURATION_IN_FRAMES}.`);
  }
  const renderDurationSeconds = renderFrameCount / FPS;
  outputDirectory = path.dirname(outputPath);
  const outputStem = path.basename(outputPath, path.extname(outputPath));
  reportPath = path.join(outputDirectory, `${outputStem}.report.json`);
  errorLogPath = path.join(outputDirectory, `${outputStem}.error.log`);
  await mkdir(outputDirectory, { recursive: true });

  if (path.extname(inputPath).toLowerCase() !== ".mov" &&
      path.extname(inputPath).toLowerCase() !== ".mp4") {
    throw new Error("Input must be an MP4 or MOV file.");
  }
  if (inputPath === outputPath || preparedProxyPath === outputPath) {
    throw new Error("The output path must not overwrite an input video or proxy.");
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
  const transcriptPayload = JSON.parse(await readFile(transcriptPath, "utf8"));
  const sourceWords = parseWhisperWordTimings(transcriptPayload);
  const wordTimings = mapWordTimingsToOutput(sourceWords, SEGMENTS, FPS);
  if (wordTimings.length === 0) {
    throw new Error("No precise Whisper word timestamps fall entirely inside the selected source clips.");
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

  const sourceFile = preparedProxyPath
    ? path.basename(preparedProxyPath)
    : "source-proxy.mp4";
  const audioFile = inputAudio ? "source-audio.wav" : null;

  console.log(`input=${inputPath}`);
  console.log(`input_sha256=${inputHashBefore}`);
  console.log(`input_probe=${JSON.stringify(inputProbe)}`);
  console.log(
    `composition=${REMOTION_RENDER_PROFILE.width}x${REMOTION_RENDER_PROFILE.height} ${FPS}fps ${DURATION_IN_FRAMES} frames; render_frames=${renderFrameCount}`,
  );
  console.log(`word_timestamps=${sourceWords.length}; mapped_sample_words=${wordTimings.length}`);

  workDirectory = await mkdtemp(path.join(outputDirectory, ".remotion-work-"));
  const publicDirectory = path.join(workDirectory, "public");
  await mkdir(publicDirectory, { recursive: true });
  const proxyPath = path.join(publicDirectory, sourceFile);
  let proxyProbe;
  if (preparedProxyPath) {
    const preparedProxyStat = await stat(preparedProxyPath);
    if (!preparedProxyStat.isFile() || preparedProxyStat.size === 0) {
      throw new Error("Prepared proxy is not a non-empty regular file.");
    }
    const preparedProxyProbe = await probe(preparedProxyPath);
    const preparedVideo = preparedProxyProbe.streams?.find(
      (stream) => stream.codec_type === "video",
    );
    const preparedDuration = Number(preparedProxyProbe.format?.duration);
    if (
      preparedVideo?.codec_name !== "h264" ||
      !preparedVideo.width ||
      !preparedVideo.height ||
      Math.abs(preparedVideo.width / preparedVideo.height - 9 / 16) > 0.001 ||
      parseRate(preparedVideo.avg_frame_rate || preparedVideo.r_frame_rate) !== FPS ||
      preparedVideo.pix_fmt !== "yuv420p" ||
      preparedVideo.color_range !== "tv" ||
      preparedVideo.color_space !== REMOTION_RENDER_PROFILE.colorSpace ||
      preparedVideo.color_transfer !== REMOTION_RENDER_PROFILE.colorSpace ||
      preparedVideo.color_primaries !== REMOTION_RENDER_PROFILE.colorSpace ||
      !Number.isFinite(preparedDuration) ||
      preparedDuration < SAMPLE_SOURCE_SECONDS
    ) {
      throw new Error(
        `Prepared proxy metadata is incompatible: ${JSON.stringify({
          duration: preparedProxyProbe.format?.duration,
          video: preparedVideo,
        })}`,
      );
    }
    const { stdout: allIntraCsv } = await run("ffprobe", [
      "-v",
      "error",
      "-select_streams",
      "v:0",
      "-show_entries",
      "frame=key_frame,pict_type",
      "-of",
      "csv=p=0",
      preparedProxyPath,
    ]);
    const allIntraInfo = parseAllIntraFrameInfo(allIntraCsv);
    if (!allIntraInfo.allIntra || allIntraInfo.frameCount < SAMPLE_SOURCE_FRAMES) {
      throw new Error(`Prepared proxy is not fully all-intra: ${JSON.stringify(allIntraInfo)}`);
    }
    await copyFile(preparedProxyPath, proxyPath);
    proxyProbe = await probe(proxyPath);
    console.log(`prepared_proxy_all_intra=${JSON.stringify(allIntraInfo)}`);
  } else {
    const proxyFilter = buildColorManagedProxyFilter(inputVideo, FPS, {
      toneMap: "mobius",
      mobiusParam: 0.5,
    });
    await run("ffmpeg", [
      "-nostdin",
      "-hide_banner",
      "-v",
      "error",
      "-threads",
      String(REMOTION_RENDER_PROFILE.offthreadVideoThreads),
      "-i",
      inputPath,
      "-map",
      "0:v:0",
      "-an",
      "-vf",
      proxyFilter,
      "-frames:v",
      String(SAMPLE_SOURCE_FRAMES),
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
    proxyProbe = await probe(proxyPath);
  }
  const proxyVideo = proxyProbe.streams?.find((stream) => stream.codec_type === "video");
  if (!proxyVideo || parseRate(proxyVideo.avg_frame_rate || proxyVideo.r_frame_rate) !== FPS) {
    throw new Error(`The color-managed proxy failed validation: ${JSON.stringify(proxyVideo)}`);
  }
  const proxyFrameCount = Math.round(Number(proxyProbe.format?.duration) * FPS);
  if (proxyFrameCount < SAMPLE_SOURCE_FRAMES) {
    throw new Error("The temporary proxy is shorter than the source ranges required by the composition.");
  }
  console.log(`proxy_probe=${JSON.stringify(proxyProbe)}`);
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
      "-t",
      SAMPLE_SOURCE_SECONDS.toFixed(3),
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
    sourceDurationInFrames: proxyFrameCount,
    audioFile,
    wordTimings,
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
    frameRange: [0, renderFrameCount - 1],
    browserExecutable: CHROMIUM,
    chromiumOptions: browserOptions,
    concurrency: REMOTION_RENDER_PROFILE.concurrency,
    maxRetries: REMOTION_RENDER_PROFILE.maxRetries,
    disallowParallelEncoding: REMOTION_RENDER_PROFILE.disallowParallelEncoding,
    offthreadVideoThreads: REMOTION_RENDER_PROFILE.offthreadVideoThreads,
    offthreadVideoCacheSizeInBytes: REMOTION_RENDER_PROFILE.offthreadVideoCacheSizeInBytes,
    crf: REMOTION_RENDER_PROFILE.outputCrf,
    x264Preset: REMOTION_RENDER_PROFILE.x264Preset,
    pixelFormat: REMOTION_RENDER_PROFILE.pixelFormat,
    colorSpace: REMOTION_RENDER_PROFILE.colorSpace,
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
    duration: Number.isFinite(outputDuration) && Math.abs(outputDuration - renderDurationSeconds) <= 0.06,
    dimensions:
      outputVideo?.width === REMOTION_RENDER_PROFILE.width &&
      outputVideo?.height === REMOTION_RENDER_PROFILE.height,
    fps: Math.abs(outputFps - FPS) < 0.01,
    videoCodec: outputVideo?.codec_name === "h264",
    pixelFormat: outputVideo?.pix_fmt === REMOTION_RENDER_PROFILE.pixelFormat,
    colorRange: outputVideo?.color_range === "tv",
    colorMetadata:
      outputVideo?.color_space === REMOTION_RENDER_PROFILE.colorSpace &&
      outputVideo?.color_transfer === REMOTION_RENDER_PROFILE.colorSpace &&
      outputVideo?.color_primaries === REMOTION_RENDER_PROFILE.colorSpace,
    audioCodec: inputAudio ? outputAudio?.codec_name === "aac" : !outputAudio,
    audioShape: inputAudio
      ? outputAudio?.sample_rate === inputAudio.sample_rate &&
        outputAudio?.channels === inputAudio.channels
      : !outputAudio,
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
  const visualCheck = await run("ffmpeg", [
    "-nostdin",
    "-hide_banner",
    "-v",
    "info",
    "-i",
    outputPath,
    "-vf",
    "fps=1,crop=iw*0.8:ih*0.6:iw*0.1:ih*0.15,signalstats,metadata=print:file=-",
    "-an",
    "-f",
    "null",
    "-",
  ]);
  const visualLog = `${visualCheck.stdout}\n${visualCheck.stderr}`;
  const visualLumaSamples = [
    ...visualLog.matchAll(/lavfi\.signalstats\.YAVG=([\d.]+)/g),
  ].map((match) => Number(match[1]));
  const visualContent = hasVisualContent(visualLumaSamples);
  if (!visualContent) {
    throw new Error(
      `Output is decodable but the central picture area is black or too dark in most sampled frames: ${JSON.stringify(visualLumaSamples)}`,
    );
  }

  const inputHashAfter = await hashFile(inputPath);
  if (inputHashAfter !== inputHashBefore) {
    throw new Error("The original source hash changed during processing.");
  }
  const report = {
    status: "success",
    input: { path: inputPath, sha256Before: inputHashBefore, sha256After: inputHashAfter },
    transcript: { path: transcriptPath, exactWordTimings: sourceWords.length, mappedToSample: wordTimings.length },
    proxy: { path: proxyPath, probe: proxyProbe, expectedSourceSeconds: SAMPLE_SOURCE_SECONDS },
    renderProfile: {
      ...REMOTION_RENDER_PROFILE,
      fps: FPS,
      renderFrameCount,
    },
    output: { path: outputPath, sizeBytes: outputStat.size, probe: outputProbe },
    validation: { ...validation, visualContent },
    visualLumaSamples,
    renderSeconds,
    sourceSizeBytes: inputStat.size,
    temporaryDirectoryBytesBeforeCleanup: await directoryBytes(outputDirectory),
    nodeProcess: {
      cpuUsageMicroseconds: process.cpuUsage(),
      rssBytes: process.memoryUsage().rss,
    },
  };
  await writeFile(
    reportPath,
    `${JSON.stringify(report, null, 2)}\n`,
    { flag: "wx" },
  );
  console.log(`render_seconds=${renderSeconds.toFixed(2)}`);
  console.log(`output=${outputPath}`);
  console.log(`output_probe=${JSON.stringify(outputProbe)}`);
  console.log(`source_unchanged=${inputHashBefore === inputHashAfter}`);
  console.log("ffmpeg_decode_check=passed");
  console.log(`visual_luma_samples=${JSON.stringify(visualLumaSamples)}`);
  succeeded = true;
} catch (error) {
  const errorText = formatError(error);
  console.error(errorText);
  if (errorLogPath) {
    await writeFile(errorLogPath, `${errorText}\n`, { flag: "wx" }).catch(() => {});
  }
  if (outputPath) await rm(outputPath, { force: true }).catch(() => {});
  process.exitCode = 1;
} finally {
  if (workDirectory) await rm(workDirectory, { recursive: true, force: true }).catch(() => {});
}

if (succeeded) console.log("render_status=success");
