import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition } from "@remotion/renderer";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import {
  copyFile,
  mkdir,
  mkdtemp,
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
  buildKeepSegments,
  buildTimedVectorEffects,
  FULL_DURATION_IN_FRAMES,
  FULL_FPS,
  FULL_HEIGHT,
  FULL_WIDTH,
} from "./src/full-edit-timeline.mjs";
import {
  buildColorManagedProxyFilter,
  parseAllIntraFrameInfo,
  REMOTION_RENDER_PROFILE,
} from "./src/render-profile.mjs";

const PACKAGE_DIR = path.dirname(fileURLToPath(import.meta.url));
const CHROMIUM = path.join(PACKAGE_DIR, "tools/chromium-offline.sh");
const EFFECT_ASSET_NAMES = new Set([
  "water-drop.svg",
  "pearl-glints.svg",
  "kiss-lips.svg",
  "kiss-heart.svg",
]);

const run = (command, args) =>
  new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (code, signal) => {
      if (code === 0) {
        resolve({ stdout, stderr });
      } else {
        const error = new Error(
          `${command} exited with code ${code ?? "null"}${signal ? ` (${signal})` : ""}`,
        );
        error.stdout = stdout;
        error.stderr = stderr;
        reject(error);
      }
    });
  });

const parseArguments = (args) => {
  const parsed = {};
  const allowed = new Set([
    "--input", "--proxy", "--transcript", "--plan", "--effects-dir", "--output",
  ]);
  for (let index = 0; index < args.length; index += 1) {
    const key = args[index];
    if (!allowed.has(key) || !args[index + 1]) {
      throw new Error(
        "Uso: render-full.mjs --input MOV --proxy PROXY --transcript JSON --plan JSON --effects-dir DIR --output MP4",
      );
    }
    parsed[key.slice(2)] = args[++index];
  }
  for (const key of ["input", "proxy", "transcript", "plan", "effects-dir", "output"]) {
    if (!parsed[key]) throw new Error(`Falta el argumento --${key}.`);
  }
  return parsed;
};

const hashFile = async (filePath) => {
  const hash = createHash("sha256");
  const { createReadStream } = await import("node:fs");
  for await (const chunk of createReadStream(filePath)) hash.update(chunk);
  return hash.digest("hex");
};

const probe = async (filePath, { countFrames = false } = {}) => {
  const { stdout } = await run("ffprobe", [
    "-v", "error",
    ...(countFrames ? ["-count_frames"] : []),
    "-show_entries",
    "format=duration,size:stream=codec_type,codec_name,profile,width,height,avg_frame_rate,nb_read_frames,sample_rate,channels,pix_fmt,color_range,color_space,color_transfer,color_primaries",
    "-of", "json", filePath,
  ]);
  return JSON.parse(stdout);
};

const parseRate = (value) => {
  const [numerator, denominator] = String(value || "0/1").split("/");
  const rate = Number(numerator) / Number(denominator);
  return Number.isFinite(rate) && rate > 0 ? rate : 0;
};

const formatError = (error) => [
  error?.stack || String(error),
  error?.stdout ? `stdout:\n${error.stdout}` : "",
  error?.stderr ? `stderr:\n${error.stderr}` : "",
].filter(Boolean).join("\n\n");

let outputPath = "";
let outputDirectory = "";
let workDirectory = "";
let errorLogPath = "";
let reportPath = "";
let inputPath = "";
let inputHashBefore = "";

try {
  const args = parseArguments(process.argv.slice(2));
  inputPath = path.resolve(args.input);
  const proxyPath = path.resolve(args.proxy);
  const transcriptPath = path.resolve(args.transcript);
  const planPath = path.resolve(args.plan);
  const effectsDirectory = path.resolve(args["effects-dir"]);
  outputPath = path.resolve(args.output);
  outputDirectory = path.dirname(outputPath);
  const outputStem = path.basename(outputPath, path.extname(outputPath));
  reportPath = path.join(outputDirectory, `${outputStem}.report.json`);
  errorLogPath = path.join(outputDirectory, `${outputStem}.error.log`);

  await mkdir(outputDirectory, { recursive: true });
  for (const filePath of [inputPath, proxyPath, transcriptPath, planPath]) {
    const info = await stat(filePath);
    if (!info.isFile() || info.size === 0) {
      throw new Error(`Se esperaba un archivo existente y no vacío: ${filePath}`);
    }
  }
  if (inputPath === outputPath || proxyPath === outputPath) {
    throw new Error("La salida no puede sobrescribir el original ni el proxy.");
  }
  try {
    await stat(outputPath);
    throw new Error(`No se sobrescribe una salida existente: ${outputPath}`);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }

  const plan = JSON.parse(await readFile(planPath, "utf8"));
  if (!Array.isArray(plan.cuts) || !Array.isArray(plan.effect_cues)) {
    throw new Error("El plan debe incluir cortes y señales de efectos vectoriales.");
  }
  const [inputProbe, proxyProbe] = await Promise.all([
    probe(inputPath),
    probe(proxyPath, { countFrames: true }),
  ]);
  const inputVideo = inputProbe.streams?.find((stream) => stream.codec_type === "video");
  const inputAudio = inputProbe.streams?.find((stream) => stream.codec_type === "audio");
  const proxyVideo = proxyProbe.streams?.find((stream) => stream.codec_type === "video");
  const proxyFrames = Number(proxyVideo?.nb_read_frames);
  if (!inputVideo || !inputAudio || inputAudio.sample_rate !== "48000" || inputAudio.channels !== 2) {
    throw new Error("El original debe tener vídeo y audio principal estéreo a 48 kHz.");
  }
  if (
    proxyVideo?.codec_name !== "h264" ||
    proxyVideo.width !== FULL_WIDTH ||
    proxyVideo.height !== FULL_HEIGHT ||
    parseRate(proxyVideo.avg_frame_rate) !== FULL_FPS ||
    proxyVideo.pix_fmt !== "yuv420p" ||
    proxyVideo.color_range !== "tv" ||
    proxyVideo.color_space !== "bt709" ||
    proxyVideo.color_transfer !== "bt709" ||
    proxyVideo.color_primaries !== "bt709" ||
    !Number.isInteger(proxyFrames) ||
    proxyFrames < 1
  ) {
    throw new Error(`Proxy incompatible con Remotion: ${JSON.stringify(proxyVideo)}`);
  }

  const { stdout: allIntraCsv } = await run("ffprobe", [
    "-v", "error", "-select_streams", "v:0",
    "-show_entries", "frame=key_frame,pict_type",
    "-of", "csv=p=0", proxyPath,
  ]);
  const allIntra = parseAllIntraFrameInfo(allIntraCsv);
  if (!allIntra.allIntra || allIntra.frameCount !== proxyFrames) {
    throw new Error(`El proxy no es totalmente All-Intra: ${JSON.stringify(allIntra)}`);
  }

  const clips = buildKeepSegments(proxyFrames, plan.cuts, FULL_FPS);
  const durationInFrames = clips.reduce((sum, clip) => sum + clip.frames, 0);
  const expectedDuration = Number(plan.expected_output_duration_seconds);
  const expectedDurationFrames = Math.round(expectedDuration * FULL_FPS);
  if (
    durationInFrames !== FULL_DURATION_IN_FRAMES ||
    Math.abs(durationInFrames - expectedDurationFrames) > 1
  ) {
    throw new Error(
      `Duración/cortes incoherentes: ${durationInFrames} frames, plan=${expectedDuration}s (${expectedDurationFrames} frames).`,
    );
  }

  const transcript = JSON.parse(await readFile(transcriptPath, "utf8"));
  const sourceWords = parseWhisperWordTimings(transcript);
  const wordTimings = mapWordTimingsToOutput(sourceWords, clips, FULL_FPS);
  if (wordTimings.length === 0) {
    throw new Error("No hay marcas temporales fiables que coincidan con los tramos conservados.");
  }
  const effects = buildTimedVectorEffects(
    plan.effect_cues,
    durationInFrames,
    FULL_FPS,
  );
  for (const effect of effects) {
    for (const asset of effect.assets) {
      if (!EFFECT_ASSET_NAMES.has(asset)) {
        throw new Error(`Recurso SVG no autorizado por el plan: ${asset}`);
      }
      const assetInfo = await stat(path.join(effectsDirectory, asset));
      if (!assetInfo.isFile() || assetInfo.size === 0) {
        throw new Error(`No existe el recurso SVG: ${asset}`);
      }
    }
  }

  inputHashBefore = await hashFile(inputPath);
  workDirectory = await mkdtemp(path.join(outputDirectory, ".remotion-full-work-"));
  const publicDirectory = path.join(workDirectory, "public");
  const publicEffectsDirectory = path.join(publicDirectory, "effects");
  await mkdir(publicEffectsDirectory, { recursive: true });
  const sourceFile = "source-proxy.mp4";
  const audioFile = "source-audio.wav";
  await copyFile(proxyPath, path.join(publicDirectory, sourceFile));
  for (const asset of new Set(effects.flatMap((effect) => effect.assets))) {
    await copyFile(
      path.join(effectsDirectory, asset),
      path.join(publicEffectsDirectory, asset),
    );
  }

  const audioPath = path.join(publicDirectory, audioFile);
  await run("ffmpeg", [
    "-nostdin", "-hide_banner", "-v", "error",
    "-i", inputPath, "-map", "0:a:0", "-vn",
    "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
    audioPath,
  ]);
  const audioProbe = await probe(audioPath);
  const extractedAudio = audioProbe.streams?.find((stream) => stream.codec_type === "audio");
  if (extractedAudio?.sample_rate !== "48000" || extractedAudio.channels !== 2) {
    throw new Error(`El audio extraído no conserva formato estéreo 48 kHz: ${JSON.stringify(extractedAudio)}`);
  }

  const serveUrl = await bundle({
    entryPoint: path.join(PACKAGE_DIR, "src/full-index.tsx"),
    publicDir: publicDirectory,
    onProgress: (progress) => console.log(`bundle_progress=${progress}%`),
  });
  const inputProps = {
    sourceFile,
    audioFile,
    sourceDurationInFrames: proxyFrames,
    durationInFrames,
    clips,
    effects,
    wordTimings,
  };
  const browserOptions = { enableMultiProcessOnLinux: true };
  const composition = await selectComposition({
    serveUrl,
    id: "MATRIX-MOTION-Remotion-Full",
    inputProps,
    browserExecutable: CHROMIUM,
    chromiumOptions: browserOptions,
  });
  if (
      composition.width !== FULL_WIDTH ||
      composition.height !== FULL_HEIGHT ||
    composition.fps !== FULL_FPS ||
    composition.durationInFrames !== durationInFrames
  ) {
    throw new Error(`Composición inesperada: ${JSON.stringify({
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
    frameRange: [0, durationInFrames - 1],
    browserExecutable: CHROMIUM,
    chromiumOptions: browserOptions,
    concurrency: 1,
    maxRetries: 0,
    disallowParallelEncoding: true,
    offthreadVideoThreads: REMOTION_RENDER_PROFILE.offthreadVideoThreads,
    offthreadVideoCacheSizeInBytes: REMOTION_RENDER_PROFILE.offthreadVideoCacheSizeInBytes,
    crf: REMOTION_RENDER_PROFILE.outputCrf,
    x264Preset: REMOTION_RENDER_PROFILE.x264Preset,
    pixelFormat: REMOTION_RENDER_PROFILE.pixelFormat,
    colorSpace: REMOTION_RENDER_PROFILE.colorSpace,
    audioCodec: "aac",
    licenseKey: null,
    logLevel: "info",
  });
  const renderSeconds = (performance.now() - renderStart) / 1000;
  const outputStat = await stat(outputPath);
  if (outputStat.size === 0) throw new Error("Remotion generó un archivo vacío.");
  const outputProbe = await probe(outputPath, { countFrames: true });
  const outputVideo = outputProbe.streams?.find((stream) => stream.codec_type === "video");
  const outputAudio = outputProbe.streams?.find((stream) => stream.codec_type === "audio");
  const outputDuration = Number(outputProbe.format?.duration);
  const validOutput =
    outputVideo?.codec_name === "h264" &&
    outputVideo.profile === "High" &&
    outputVideo.width === FULL_WIDTH &&
    outputVideo.height === FULL_HEIGHT &&
    parseRate(outputVideo.avg_frame_rate) === FULL_FPS &&
    outputVideo.pix_fmt === "yuv420p" &&
    outputVideo.color_range === "tv" &&
    outputVideo.color_space === "bt709" &&
    outputVideo.color_transfer === "bt709" &&
    outputVideo.color_primaries === "bt709" &&
    Number(outputVideo.nb_read_frames) === durationInFrames &&
    Math.abs(outputDuration - durationInFrames / FULL_FPS) <= 0.06 &&
    outputAudio?.codec_name === "aac" &&
    outputAudio.sample_rate === "48000" &&
    outputAudio.channels === 2;
  if (!validOutput) {
    throw new Error(`El MP4 final no superó las validaciones: ${JSON.stringify(outputProbe)}`);
  }
  await run("ffmpeg", [
    "-nostdin", "-hide_banner", "-v", "error",
    "-i", outputPath, "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-",
  ]);
  const inputHashAfter = await hashFile(inputPath);
  if (inputHashAfter !== inputHashBefore) {
    throw new Error("El hash del MOV original cambió durante el procesamiento.");
  }

  const report = {
    status: "verified",
    input: { path: inputPath, sha256: inputHashBefore, probe: inputProbe },
    proxy: {
      path: proxyPath,
      probe: proxyProbe,
      allIntra,
      colorFilter: buildColorManagedProxyFilter(inputVideo, FULL_FPS, {
        toneMap: "direct",
        width: FULL_WIDTH,
        height: FULL_HEIGHT,
      }),
      encoding: {
        codec: "libx264",
        preset: "ultrafast",
        crf: REMOTION_RENDER_PROFILE.proxyCrf,
        gopFrames: 1,
        pixFmt: REMOTION_RENDER_PROFILE.pixelFormat,
        colorSpace: REMOTION_RENDER_PROFILE.colorSpace,
      },
    },
    edit: {
      plan: planPath,
      sourceFrames: proxyFrames,
      outputFrames: durationInFrames,
      fps: FULL_FPS,
      durationSeconds: durationInFrames / FULL_FPS,
      clips,
      sourceWordTimings: sourceWords.length,
      mappedWordTimings: wordTimings.length,
      effects,
    },
    render: {
      path: outputPath,
      sizeBytes: outputStat.size,
      seconds: renderSeconds,
      settings: {
        codec: "h264",
        crf: REMOTION_RENDER_PROFILE.outputCrf,
        preset: REMOTION_RENDER_PROFILE.x264Preset,
        profile: outputVideo.profile,
        pixelFormat: REMOTION_RENDER_PROFILE.pixelFormat,
        colorSpace: REMOTION_RENDER_PROFILE.colorSpace,
        concurrency: 1,
        maxRetries: 0,
        offthreadVideoThreads: REMOTION_RENDER_PROFILE.offthreadVideoThreads,
        offthreadVideoCacheSizeInBytes: REMOTION_RENDER_PROFILE.offthreadVideoCacheSizeInBytes,
      },
      probe: outputProbe,
      fullDecode: "passed",
      sourceHashUnchanged: inputHashAfter === inputHashBefore,
    },
  };
  await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  console.log(`output=${outputPath}`);
  console.log(`report=${reportPath}`);
  console.log(`render_seconds=${renderSeconds.toFixed(2)}`);
  console.log(`output_probe=${JSON.stringify(outputProbe)}`);
} catch (error) {
  const message = formatError(error);
  console.error(message);
  if (errorLogPath) await writeFile(errorLogPath, `${message}\n`).catch(() => {});
  process.exitCode = 1;
} finally {
  if (workDirectory) await rm(workDirectory, { recursive: true, force: true }).catch(() => {});
}
