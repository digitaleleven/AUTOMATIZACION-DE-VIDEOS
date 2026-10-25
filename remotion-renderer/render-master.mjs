import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition } from "@remotion/renderer";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { createReadStream } from "node:fs";
import {
  copyFile,
  mkdir,
  mkdtemp,
  readFile,
  rm,
  stat,
  symlink,
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
  buildJumpFrames,
  buildMasterFilterComplex,
  buildSoundEffectCues,
} from "./src/master-assembly.mjs";
import {
  MASTER_OVERLAY_PROFILE,
  resolveRenderFrameCount,
  supportsAlphaOverlayPixelFormat,
} from "./src/master-render-profile.mjs";
import { buildColorManagedProxyFilter } from "./src/render-profile.mjs";

const PACKAGE_DIR = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(PACKAGE_DIR, "..");
const CHROMIUM = path.join(PACKAGE_DIR, "tools/chromium-offline.sh");
const EFFECT_ASSET_NAMES = new Set([
  "water-drop.svg",
  "pearl-glints.svg",
  "kiss-lips.svg",
  "kiss-heart.svg",
]);
const DEFAULTS = {
  input: path.join(PROJECT_ROOT, "generated-artifacts/drive-inspection/IMG_1254.MOV"),
  transcript: path.join(
    PROJECT_ROOT,
    "generated-artifacts/drive-inspection/q03a/transcription-corrected.json",
  ),
  plan: path.join(PROJECT_ROOT, "generated-artifacts/drive-inspection/mm004/mm004-plan.json"),
  effectsDir: path.join(PROJECT_ROOT, "generated-artifacts/drive-inspection/mm004/effects"),
  output: path.join(PROJECT_ROOT, "outputs/IMG_1254-MATRIX-MOTION-v3-PRO-MASTER.mp4"),
};

const run = (command, args, { cwd } = {}) =>
  new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (code, signal) => {
      if (code === 0) resolve({ stdout, stderr });
      else {
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
  const allowed = new Set(["--input", "--transcript", "--plan", "--effects-dir", "--output", "--show-subtitles", "--render-frames"]);
  for (let index = 0; index < args.length; index += 1) {
    const key = args[index];
    if (!allowed.has(key) || !args[index + 1]) {
      throw new Error(
        "Uso: render-master.mjs [--input MOV] [--transcript JSON] [--plan JSON] " +
        "[--effects-dir DIR] [--output MP4] [--show-subtitles true|false] " +
        "[--render-frames COUNT]",
      );
    }
    parsed[key] = args[index + 1];
    index += 1;
  }
  return {
    input: path.resolve(parsed["--input"] || DEFAULTS.input),
    transcript: path.resolve(parsed["--transcript"] || DEFAULTS.transcript),
    plan: path.resolve(parsed["--plan"] || DEFAULTS.plan),
    effectsDir: path.resolve(parsed["--effects-dir"] || DEFAULTS.effectsDir),
    output: path.resolve(parsed["--output"] || DEFAULTS.output),
    showSubtitlesArg: parsed["--show-subtitles"],
    renderFramesArg: parsed["--render-frames"],
  };
};

const probe = async (filePath, countFrames = false) => {
  const args = ["-v", "error"];
  if (countFrames) args.push("-count_frames");
  args.push("-show_streams", "-show_format", "-of", "json", filePath);
  const { stdout } = await run("ffprobe", args);
  return JSON.parse(stdout);
};

const hashFile = async (filePath) => {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(filePath)) hash.update(chunk);
  return hash.digest("hex");
};

const makeSfxAssets = async (publicDirectory) => {
  const directory = path.join(publicDirectory, "sfx");
  await mkdir(directory, { recursive: true });
  const outputs = [
    [
      "swoosh.wav",
      "anoisesrc=color=white:sample_rate=48000:duration=0.34",
      "highpass=f=550,lowpass=f=6500,afade=t=in:st=0:d=0.06," +
        "afade=t=out:st=0.10:d=0.24,volume=0.16",
    ],
    [
      "pop.wav",
      "sine=frequency=680:sample_rate=48000:duration=0.16",
      "afade=t=out:st=0.015:d=0.145,volume=0.16",
    ],
    [
      "ding.wav",
      "aevalsrc=0.4*sin(2*PI*880*t)*exp(-16*t)+0.2*sin(2*PI*1320*t)*exp(-18*t):s=48000:d=0.28",
      "volume=0.16",
    ],
  ];
  for (const [filename, source, filter] of outputs) {
    await run("ffmpeg", [
      "-nostdin", "-y", "-hide_banner", "-v", "error",
      "-f", "lavfi", "-i", source,
      "-af", filter, "-ac", "2", "-c:a", "pcm_s16le",
      path.join(directory, filename),
    ]);
  }
};

const readSubtitlePreference = async (explicit) => {
  if (explicit !== undefined) {
    if (explicit !== "true" && explicit !== "false") {
      throw new Error("--show-subtitles solo acepta true o false.");
    }
    return explicit === "true";
  }
  try {
    const style = JSON.parse(
      await readFile(path.join(PROJECT_ROOT, "video_style.json"), "utf8"),
    );
    return typeof style.show_subtitles === "boolean" ? style.show_subtitles : true;
  } catch (error) {
    if (error?.code === "ENOENT") return true;
    throw error;
  }
};

const parseRate = (rate) => {
  const [numerator, denominator] = String(rate || "0/1").split("/").map(Number);
  return denominator ? numerator / denominator : 0;
};

const formatError = (error) => [
  error?.message || String(error),
  error?.stderr ? `stderr:\n${error.stderr}` : "",
].filter(Boolean).join("\n");

let workDirectory;
let shortTmpLink;
let previousTmpDir;
let outputPath;
let errorLogPath;

try {
  const options = parseArguments(process.argv.slice(2));
  const inputPath = options.input;
  const transcriptPath = options.transcript;
  const planPath = options.plan;
  const effectsDirectory = options.effectsDir;
  outputPath = options.output;
  const outputDirectory = path.dirname(outputPath);
  const reportPath = outputPath.replace(/\.mp4$/iu, ".report.json");
  errorLogPath = outputPath.replace(/\.mp4$/iu, ".error.log");
  const showSubtitles = await readSubtitlePreference(options.showSubtitlesArg);

  await mkdir(outputDirectory, { recursive: true });
  for (const filePath of [inputPath, transcriptPath, planPath]) {
    const info = await stat(filePath);
    if (!info.isFile() || info.size === 0) {
      throw new Error(`Se esperaba un archivo existente y no vacío: ${filePath}`);
    }
  }
  if (inputPath === outputPath || path.extname(outputPath).toLowerCase() !== ".mp4") {
    throw new Error("La salida debe ser un MP4 nuevo y no puede sobrescribir la fuente.");
  }
  for (const filePath of [outputPath, reportPath, errorLogPath]) {
    try {
      await stat(filePath);
      throw new Error(`Se rechaza sobrescribir un archivo existente: ${filePath}`);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }

  const plan = JSON.parse(await readFile(planPath, "utf8"));
  if (!Array.isArray(plan.cuts) || !Array.isArray(plan.effect_cues)) {
    throw new Error("El plan debe incluir cortes y señales de elementos vectoriales.");
  }
  const inputProbe = await probe(inputPath);
  const inputVideo = inputProbe.streams?.find((stream) => stream.codec_type === "video");
  const inputAudio = inputProbe.streams?.find(
    (stream) =>
      stream.codec_type === "audio" &&
      stream.codec_name === "aac" &&
      stream.sample_rate === "48000" &&
      stream.channels === 2,
  );
  const sourceDuration = Number(inputProbe.format?.duration);
  if (!inputVideo || !inputAudio || !Number.isFinite(sourceDuration) || sourceDuration <= 0) {
    throw new Error("Se requiere el MOV original, vídeo detectable y pista AAC estéreo de 48 kHz.");
  }
  if (
    inputVideo.color_range !== "tv" ||
    inputVideo.color_space !== "bt2020nc" ||
    inputVideo.color_transfer !== "arib-std-b67" ||
    inputVideo.color_primaries !== "bt2020" ||
    parseRate(inputVideo.avg_frame_rate) < FULL_FPS
  ) {
    throw new Error(`El original no coincide con el perfil iPhone HLG esperado: ${JSON.stringify(inputVideo)}`);
  }

  const sourceFrames = Math.round(sourceDuration * FULL_FPS);
  const clips = buildKeepSegments(sourceFrames, plan.cuts, FULL_FPS);
  const durationInFrames = clips.reduce((sum, clip) => sum + clip.frames, 0);
  const expectedFrames = Math.round(Number(plan.expected_output_duration_seconds) * FULL_FPS);
  if (
    durationInFrames !== FULL_DURATION_IN_FRAMES ||
    Math.abs(durationInFrames - expectedFrames) > 1
  ) {
    throw new Error(
      `La duración/cortes no coinciden con el plan: ${durationInFrames} fotogramas frente a ${expectedFrames}.`,
    );
  }
  const renderFrameCount = resolveRenderFrameCount(
    options.renderFramesArg,
    durationInFrames,
  );
  const renderDurationSeconds = renderFrameCount / FULL_FPS;

  const transcript = JSON.parse(await readFile(transcriptPath, "utf8"));
  const sourceWords = parseWhisperWordTimings(transcript);
  const wordTimings = mapWordTimingsToOutput(sourceWords, clips, FULL_FPS);
  if (wordTimings.length === 0) {
    throw new Error("No hay marcas de palabra temporales válidas para la edición.");
  }
  const effects = buildTimedVectorEffects(plan.effect_cues, durationInFrames, FULL_FPS);
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
  const jumpFrames = buildJumpFrames(
    clips,
    wordTimings,
    plan.effect_cues,
    FULL_FPS,
  ).filter((frame) => frame < durationInFrames);
  const soundEffects = buildSoundEffectCues(
    clips,
    effects,
    wordTimings,
    plan.effect_cues,
    FULL_FPS,
  ).filter((cue) => cue.startFrame + cue.durationFrames <= durationInFrames);
  const soundEffectsInRender = soundEffects.filter(
    (cue) => cue.startFrame < renderFrameCount,
  );

  const workRoot = path.join(PROJECT_ROOT, "generated-artifacts/drive-inspection/remotion");
  await mkdir(workRoot, { recursive: true });
  workDirectory = await mkdtemp(path.join(workRoot, ".master-work-"));
  const publicDirectory = path.join(workDirectory, "public");
  const publicEffectsDirectory = path.join(publicDirectory, "effects");
  await mkdir(publicEffectsDirectory, { recursive: true });
  for (const asset of new Set(effects.flatMap((effect) => effect.assets))) {
    await copyFile(path.join(effectsDirectory, asset), path.join(publicEffectsDirectory, asset));
  }
  await makeSfxAssets(publicDirectory);
  const overlayPath = path.join(workDirectory, "graphics-overlay.mov");
  const tempDirectory = path.join(workDirectory, "remotion-tmp");
  await mkdir(tempDirectory, { recursive: true });
  shortTmpLink = await mkdtemp("/tmp/mm-");
  await rm(shortTmpLink, { recursive: true, force: true });
  await symlink(tempDirectory, shortTmpLink, "dir");
  previousTmpDir = process.env.TMPDIR;
  process.env.TMPDIR = shortTmpLink;

  const serveUrl = await bundle({
    entryPoint: path.join(PACKAGE_DIR, "src/full-index.tsx"),
    publicDir: publicDirectory,
    onProgress: (progress) => console.log(`overlay_bundle_progress=${progress}%`),
  });
  const inputProps = {
    durationInFrames,
    effects,
    wordTimings,
    showSubtitles,
    soundEffects: soundEffectsInRender,
  };
  const browserOptions = { enableMultiProcessOnLinux: true };
  const composition = await selectComposition({
    serveUrl,
    id: "MATRIX-MOTION-Master-Overlay",
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
    throw new Error(`La composición gráfica no coincide con el plan: ${JSON.stringify({
      width: composition.width,
      height: composition.height,
      fps: composition.fps,
      durationInFrames: composition.durationInFrames,
    })}`);
  }

  const overlayStarted = performance.now();
  await renderMedia({
    composition,
    serveUrl,
    inputProps,
    ...MASTER_OVERLAY_PROFILE,
    outputLocation: overlayPath,
    frameRange: [0, renderFrameCount - 1],
    browserExecutable: CHROMIUM,
    chromiumOptions: browserOptions,
    logLevel: "info",
  });
  const overlaySeconds = (performance.now() - overlayStarted) / 1000;
  const overlayProbe = await probe(overlayPath, true);
  const overlayVideo = overlayProbe.streams?.find((stream) => stream.codec_type === "video");
  const overlayAudio = overlayProbe.streams?.find((stream) => stream.codec_type === "audio");
  const overlayAudioValid =
    soundEffectsInRender.length > 0
      ? overlayAudio?.codec_name === "pcm_s16le" &&
        overlayAudio.sample_rate === "48000" &&
        overlayAudio.channels === 2
      : true;
  if (
    overlayVideo?.codec_name !== "prores" ||
    overlayVideo.width !== FULL_WIDTH ||
    overlayVideo.height !== FULL_HEIGHT ||
    parseRate(overlayVideo.avg_frame_rate) !== FULL_FPS ||
    !supportsAlphaOverlayPixelFormat(overlayVideo.pix_fmt) ||
    Number(overlayVideo.nb_read_frames) !== renderFrameCount ||
    Math.abs(Number(overlayProbe.format?.duration) - renderDurationSeconds) > 0.06 ||
    !overlayAudioValid
  ) {
    throw new Error(`La capa Remotion no conserva alpha/audio/formato: ${JSON.stringify(overlayProbe)}`);
  }

  const colorFilter = buildColorManagedProxyFilter(inputVideo, FULL_FPS, {
    toneMap: "direct",
    width: FULL_WIDTH,
    height: FULL_HEIGHT,
    visualPass: renderFrameCount < durationInFrames,
  });
  const filterComplex = buildMasterFilterComplex({
    clips,
    sourceAudioStreamIndex: inputAudio.index,
    colorFilter,
    jumpFrames,
    includeSoundEffects: soundEffectsInRender.length > 0,
    width: FULL_WIDTH,
    height: FULL_HEIGHT,
    fps: FULL_FPS,
  });
  const inputHashBefore = await hashFile(inputPath);
  const masterStarted = performance.now();
  await run("ffmpeg", [
    "-nostdin", "-n", "-hide_banner", "-loglevel", "error",
    "-i", inputPath,
    "-i", overlayPath,
    "-filter_complex", filterComplex,
    "-map", "[vout]", "-map", "[aout]",
    "-t", renderDurationSeconds.toFixed(3),
    "-frames:v", String(renderFrameCount),
    "-c:v", "libx264", "-preset", "medium", "-crf", "15",
    "-profile:v", "high", "-pix_fmt", "yuv420p",
    "-fps_mode", "cfr",
    "-color_range", "tv", "-colorspace", "bt709",
    "-color_primaries", "bt709", "-color_trc", "bt709",
    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
    "-movflags", "+faststart",
    outputPath,
  ]);
  const masterSeconds = (performance.now() - masterStarted) / 1000;
  const outputStat = await stat(outputPath);
  if (outputStat.size === 0) throw new Error("FFmpeg produjo un MP4 vacío.");
  const outputProbe = await probe(outputPath, true);
  const outputVideo = outputProbe.streams?.find((stream) => stream.codec_type === "video");
  const outputAudio = outputProbe.streams?.find((stream) => stream.codec_type === "audio");
  const outputDuration = Number(outputProbe.format?.duration);
  const expectedDuration = renderDurationSeconds;
  const outputVideoBitrateMbps = Number(outputVideo?.bit_rate) / 1_000_000;
  const outputContainerBitrateMbps =
    (outputStat.size * 8) / (expectedDuration * 1_000_000);
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
    Number(outputVideo.nb_read_frames) === renderFrameCount &&
    Math.abs(outputDuration - expectedDuration) <= 0.06 &&
    outputAudio?.codec_name === "aac" &&
    outputAudio.sample_rate === "48000" &&
    outputAudio.channels === 2;
  if (!validOutput) {
    throw new Error(`El máster MP4 no superó las validaciones: ${JSON.stringify(outputProbe)}`);
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
    renderMode: renderFrameCount < durationInFrames ? "preview" : "full",
    requestedFrames: renderFrameCount,
    input: {
      path: inputPath,
      durationSeconds: sourceDuration,
      videoStream: inputVideo,
      audioStream: inputAudio,
      sha256: inputHashBefore,
    },
    pipeline: {
      stage1: {
        renderer: "Remotion 4.0.538",
        purpose: "gráficos transparentes, subtítulos y SFX; sin decodificar el vídeo fuente",
        overlayCodec: "ProRes 4444",
        pixelFormat: overlayVideo.pix_fmt,
        frames: renderFrameCount,
        fullCompositionFrames: durationInFrames,
        seconds: overlaySeconds,
        showSubtitles,
        wordTimings: wordTimings.length,
        effects,
        soundEffects: soundEffectsInRender,
        scheduledSoundEffects: soundEffects.length,
        jumpFrames,
        zoom: "instantáneo, una fotograma, máximo 1.06x; sin zoom progresivo",
      },
      stage2: {
        renderer: "FFmpeg",
        source: "MOV HEVC HLG original",
        colorFilter,
        codec: "H.264 High",
        crf: 15,
        preset: "medium",
        targetBitrateMbps: "variable with CRF; measured value is reported below",
        width: FULL_WIDTH,
        height: FULL_HEIGHT,
        fps: FULL_FPS,
        pixelFormat: "yuv420p",
        colorRange: "limited",
        colorSpace: "BT.709",
        seconds: outputDuration,
        videoBitrateMbps: Number.isFinite(outputVideoBitrateMbps)
          ? outputVideoBitrateMbps
          : null,
        containerBitrateMbps: outputContainerBitrateMbps,
      },
    },
    output: {
      path: outputPath,
      sizeBytes: outputStat.size,
      durationSeconds: outputDuration,
      frames: Number(outputVideo.nb_read_frames),
      probe: outputProbe,
      fullDecode: "passed",
      sourceHashUnchanged: inputHashBefore === inputHashAfter,
    },
  };
  await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  console.log(`output=${outputPath}`);
  console.log(`report=${reportPath}`);
  console.log(`render_mode=${renderFrameCount < durationInFrames ? "preview" : "full"}`);
  console.log(`render_frames=${renderFrameCount}`);
  console.log(`remotion_overlay_seconds=${overlaySeconds.toFixed(2)}`);
  console.log(`ffmpeg_master_seconds=${masterSeconds.toFixed(2)}`);
  console.log(`output_probe=${JSON.stringify(outputProbe)}`);
} catch (error) {
  const message = formatError(error);
  console.error(message);
  if (errorLogPath) await writeFile(errorLogPath, `${message}\n`).catch(() => {});
  process.exitCode = 1;
} finally {
  if (previousTmpDir === undefined) delete process.env.TMPDIR;
  else process.env.TMPDIR = previousTmpDir;
  if (shortTmpLink) await rm(shortTmpLink, { recursive: true, force: true }).catch(() => {});
  if (workDirectory) await rm(workDirectory, { recursive: true, force: true }).catch(() => {});
}
