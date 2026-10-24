import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition } from "@remotion/renderer";
import { createHash, randomUUID } from "node:crypto";
import {
  copyFile,
  mkdir,
  readFile,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { constants as fsConstants, createReadStream } from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";

const PACKAGE_DIR = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(PACKAGE_DIR, "..");
const INPUT_PATH = path.join(
  PROJECT_ROOT,
  "data/incoming/608c74727e18449da2899f75d426f8c9.mov",
);
const EXPECTED_INPUT_SHA256 =
  "8fbe91573d1867d185adc10e78cf57b6336b0db33dd2cf361a70eda2896861e5";
const ARTIFACT_ROOT = path.join(
  PROJECT_ROOT,
  "generated-artifacts/remotion",
);
const FPS = 50;
const EXPECTED_FRAMES = 285;
const EXPECTED_DURATION = EXPECTED_FRAMES / FPS;
const SOURCE_FRAMES = 941;
const CHROMIUM = path.join(PACKAGE_DIR, "tools/chromium-offline.sh");

let runDir;
let logPath;

const log = async (message) => {
  const line = `[${new Date().toISOString()}] ${message}`;
  console.log(line);
  if (logPath) {
    await writeFile(logPath, `${line}\n`, { flag: "a" });
  }
};

const run = (command, args, options = {}) =>
  new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: ["ignore", "pipe", "pipe"],
      ...options,
    });
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
    child.on("error", reject);
    child.on("close", (code, signal) => {
      if (code === 0) {
        resolve({ stdout, stderr, code, signal });
      } else {
        reject(
          new Error(
            `${command} terminó con código ${code ?? "null"}${
              signal ? ` (${signal})` : ""
            }: ${stderr.slice(-6000)}`,
          ),
        );
      }
    });
  });

const sha256File = (filePath) =>
  new Promise((resolve, reject) => {
    const hash = createHash("sha256");
    const stream = createReadStream(filePath);
    stream.on("error", reject);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("end", () => resolve(hash.digest("hex")));
  });

const readCgroupMetric = async (filePath, key = null) => {
  try {
    const text = await readFile(filePath, "utf8");
    if (!key) {
      return Number.parseInt(text.trim(), 10);
    }
    const line = text
      .split("\n")
      .find((candidate) => candidate.startsWith(`${key} `));
    return line ? Number.parseInt(line.split(/\s+/)[1], 10) : null;
  } catch {
    return null;
  }
};

const readCpuQuota = async () => {
  try {
    const [quotaText, periodText] = (await readFile(
      "/sys/fs/cgroup/cpu.max",
      "utf8",
    ))
      .trim()
      .split(/\s+/);
    if (quotaText === "max") {
      return null;
    }
    return Number(quotaText) / Number(periodText);
  } catch {
    return null;
  }
};

const startResourceMonitor = async () => {
  const cpuLimitCores = await readCpuQuota();
  let lastUsage = await readCgroupMetric(
    "/sys/fs/cgroup/cpu.stat",
    "usage_usec",
  );
  let lastTime = performance.now();
  let peakMemoryBytes =
    (await readCgroupMetric("/sys/fs/cgroup/memory.current")) ?? 0;
  let maxCpuCores = 0;

  const sample = async () => {
    const [usage, memory] = await Promise.all([
      readCgroupMetric("/sys/fs/cgroup/cpu.stat", "usage_usec"),
      readCgroupMetric("/sys/fs/cgroup/memory.current"),
    ]);
    const now = performance.now();
    if (usage !== null && lastUsage !== null) {
      const elapsedUsec = (now - lastTime) * 1000;
      if (elapsedUsec > 0) {
        maxCpuCores = Math.max(
          maxCpuCores,
          Math.max(0, usage - lastUsage) / elapsedUsec,
        );
      }
    }
    if (memory !== null) {
      peakMemoryBytes = Math.max(peakMemoryBytes, memory);
    }
    lastUsage = usage;
    lastTime = now;
  };

  const timer = setInterval(() => {
    sample().catch(() => {});
  }, 250);

  return {
    async stop() {
      clearInterval(timer);
      await sample();
      return {
        cpuLimitCores,
        maxCpuCores: Number(maxCpuCores.toFixed(3)),
        maxCpuPercentOfLimit:
          cpuLimitCores && cpuLimitCores > 0
            ? Number(((maxCpuCores / cpuLimitCores) * 100).toFixed(1))
            : null,
        peakCgroupMemoryBytes: peakMemoryBytes,
      };
    },
  };
};

const getProbe = async (filePath) => {
  const { stdout } = await run("ffprobe", [
    "-v",
    "error",
    "-count_frames",
    "-show_entries",
    "stream=index,codec_type,codec_name,width,height,avg_frame_rate,nb_read_frames,sample_rate,channels,duration",
    "-show_entries",
    "format=duration,size,format_name",
    "-of",
    "json",
    filePath,
  ]);
  return JSON.parse(stdout);
};

const parseFrameRate = (value) => {
  if (typeof value !== "string" || !value.includes("/")) {
    return null;
  }
  const [numerator, denominator] = value.split("/").map(Number);
  return denominator ? numerator / denominator : null;
};

const validateOutput = (probe) => {
  const video = probe.streams?.find((stream) => stream.codec_type === "video");
  const audio = probe.streams?.find((stream) => stream.codec_type === "audio");
  const duration = Number(probe.format?.duration);
  const fps = parseFrameRate(video?.avg_frame_rate);
  const failures = [];

  if (!video) failures.push("falta la pista de vídeo");
  if (!audio) failures.push("falta la pista de audio");
  if (video?.codec_name !== "h264") failures.push("el vídeo no es H.264");
  if (video?.width !== 1080 || video?.height !== 1920) {
    failures.push("la resolución no es 1080×1920");
  }
  if (Math.abs((fps ?? 0) - FPS) > 0.001) {
    failures.push("la frecuencia no es 50 fps");
  }
  if (audio?.codec_name !== "aac") failures.push("el audio no es AAC");
  if (Math.abs(duration - EXPECTED_DURATION) > 0.06) {
    failures.push(
      `la duración ${duration.toFixed(3)}s difiere de ${EXPECTED_DURATION}s`,
    );
  }
  if (Number(video?.nb_read_frames) !== EXPECTED_FRAMES) {
    failures.push(
      `el recuento de fotogramas es ${video?.nb_read_frames ?? "desconocido"}, ` +
        `se esperaban ${EXPECTED_FRAMES}`,
    );
  }

  return { valid: failures.length === 0, failures, video, audio, duration, fps };
};

const correlationAtOffsets = (reference, candidate, maxLagSamples) => {
  const sampleCount = Math.min(reference.length, candidate.length);
  if (sampleCount < 1000) {
    return { correlation: null, lagSamples: null };
  }

  const mean = (samples) => {
    let total = 0;
    for (let index = 0; index < sampleCount; index += 1) {
      total += samples[index];
    }
    return total / sampleCount;
  };
  const referenceMean = mean(reference);
  const candidateMean = mean(candidate);
  let best = { correlation: -1, lagSamples: null };

  for (let lag = -maxLagSamples; lag <= maxLagSamples; lag += 1) {
    const referenceStart = lag < 0 ? -lag : 0;
    const candidateStart = lag > 0 ? lag : 0;
    const count = Math.min(
      sampleCount - referenceStart,
      sampleCount - candidateStart,
    );
    let dot = 0;
    let referenceEnergy = 0;
    let candidateEnergy = 0;
    for (let index = 0; index < count; index += 1) {
      const left = reference[referenceStart + index] - referenceMean;
      const right = candidate[candidateStart + index] - candidateMean;
      dot += left * right;
      referenceEnergy += left * left;
      candidateEnergy += right * right;
    }
    const denominator = Math.sqrt(referenceEnergy * candidateEnergy);
    const correlation = denominator ? dot / denominator : 0;
    if (correlation > best.correlation) {
      best = { correlation, lagSamples: lag };
    }
  }

  return best;
};

const extractAudioWindow = async (input, seekSeconds, durationSeconds, output) => {
  await run("ffmpeg", [
    "-nostdin",
    "-hide_banner",
    "-loglevel",
    "error",
    "-i",
    input,
    "-ss",
    seekSeconds.toFixed(3),
    "-t",
    durationSeconds.toFixed(3),
    "-vn",
    "-ac",
    "1",
    "-ar",
    "11025",
    "-f",
    "s16le",
    "-y",
    output,
  ]);
  const bytes = await readFile(output);
  const samples = new Int16Array(bytes.length >> 1);
  for (let index = 0; index < samples.length; index += 1) {
    samples[index] = bytes.readInt16LE(index * 2);
  }
  return samples;
};

const verifyAudioAlignment = async ({
  originalAudioPath,
  outputVideoPath,
  runDirectory,
}) => {
  const sourceStartSeconds = 5.6;
  const outputStartSeconds = 2.2;
  const durationSeconds = 0.6;
  const sampleRate = 11025;
  const expectedPath = path.join(runDirectory, "audio-sync-reference.raw");
  const actualPath = path.join(runDirectory, "audio-sync-output.raw");
  const [reference, candidate] = await Promise.all([
    extractAudioWindow(
      originalAudioPath,
      sourceStartSeconds,
      durationSeconds,
      expectedPath,
    ),
    extractAudioWindow(
      outputVideoPath,
      outputStartSeconds,
      durationSeconds,
      actualPath,
    ),
  ]);
  await Promise.all([rm(expectedPath, { force: true }), rm(actualPath, { force: true })]);

  const maxLagSamples = Math.round(sampleRate * 0.05);
  const match = correlationAtOffsets(reference, candidate, maxLagSamples);
  const correlation =
    match.correlation === null ? null : Number(match.correlation.toFixed(4));
  const lagMilliseconds =
    match.lagSamples === null
      ? null
      : Number(((match.lagSamples / sampleRate) * 1000).toFixed(2));

  if (correlation === null || correlation < 0.65) {
    return {
      status: "inconclusive",
      correlation,
      lagMilliseconds,
      sample: {
        sourceStartSeconds,
        outputStartSeconds,
        durationSeconds,
        sampleRate,
      },
      note: "La ventana no dio una correlación suficiente para confirmar sincronía.",
    };
  }

  return {
    status: Math.abs(lagMilliseconds) <= 35 ? "passed" : "failed",
    correlation,
    lagMilliseconds,
    sample: {
      sourceStartSeconds,
      outputStartSeconds,
      durationSeconds,
      sampleRate,
    },
    note:
      "Comparación de señal PCM mono en una ventana interior del segundo segmento, fuera de los fundidos.",
  };
};

const main = async () => {
  const runId = `0415-phase1-${new Date()
    .toISOString()
    .replaceAll(":", "-")}-${process.pid}-${randomUUID().slice(0, 8)}`;
  await mkdir(ARTIFACT_ROOT, { recursive: true });
  runDir = path.join(ARTIFACT_ROOT, runId);
  await mkdir(runDir, { recursive: false });
  logPath = path.join(runDir, "run.log");
  const outputPath = path.join(runDir, "0415-remotion-phase1.mp4");
  const publicDir = path.join(runDir, "public");
  const bundleDir = path.join(runDir, "bundle");

  await log(`run_id=${runId}`);
  await log(`input=${INPUT_PATH}`);
  await log(`output=${outputPath}`);
  await log("No se configura licenseKey; el render es local/server-side.");

  const originalStat = await stat(INPUT_PATH);
  if (!originalStat.isFile()) {
    throw new Error("La entrada no es un archivo regular.");
  }
  const originalHashBefore = await sha256File(INPUT_PATH);
  if (originalHashBefore !== EXPECTED_INPUT_SHA256) {
    throw new Error(
      `El SHA-256 de 0415.mov no coincide con el valor auditado; ` +
        `se cancela antes de renderizar. SHA actual: ${originalHashBefore}`,
    );
  }

  const inputProbe = await getProbe(INPUT_PATH);
  const sourceVideo = inputProbe.streams?.find(
    (stream) => stream.codec_type === "video",
  );
  const sourceAudio = inputProbe.streams?.find(
    (stream) => stream.codec_type === "audio",
  );
  if (
    sourceVideo?.codec_name !== "h264" ||
    sourceVideo?.width !== 2160 ||
    sourceVideo?.height !== 3840 ||
    parseFrameRate(sourceVideo?.avg_frame_rate) !== FPS ||
    sourceAudio?.codec_name !== "aac"
  ) {
    throw new Error(
      "0415.mov ya no coincide con el perfil H.264/AAC vertical de 50 fps auditado.",
    );
  }
  if (Number(sourceVideo?.nb_read_frames) !== SOURCE_FRAMES) {
    throw new Error(
      `El recuento de fotogramas de origen cambió: ${sourceVideo?.nb_read_frames}.`,
    );
  }

  await mkdir(publicDir, { recursive: false });
  const copiedVideoPath = path.join(publicDir, "0415-source.mov");
  const copiedAudioPath = path.join(publicDir, "0415-audio.wav");
  await copyFile(INPUT_PATH, copiedVideoPath, fsConstants.COPYFILE_EXCL);
  const copiedHash = await sha256File(copiedVideoPath);
  if (copiedHash !== originalHashBefore) {
    throw new Error("La copia de trabajo no coincide con el original.");
  }

  await log("Extrayendo temporalmente el audio PCM local desde una copia.");
  await run("ffmpeg", [
    "-nostdin",
    "-hide_banner",
    "-loglevel",
    "error",
    "-i",
    copiedVideoPath,
    "-map",
    "0:a:0",
    "-vn",
    "-c:a",
    "pcm_s16le",
    "-ar",
    "44100",
    "-ac",
    "2",
    "-n",
    copiedAudioPath,
  ]);

  const audioProbe = await getProbe(copiedAudioPath);
  if (
    !audioProbe.streams?.some(
      (stream) =>
        stream.codec_type === "audio" &&
        stream.codec_name === "pcm_s16le" &&
        stream.sample_rate === "44100" &&
        stream.channels === 2,
    )
  ) {
    throw new Error("La extracción local de audio PCM no pasó su validación.");
  }

  await log("Creando bundle local de Remotion.");
  const serveUrl = await bundle({
    entryPoint: path.join(PACKAGE_DIR, "src/index.tsx"),
    rootDir: PACKAGE_DIR,
    publicDir,
    outDir: bundleDir,
    onProgress: (progress) => {
      if (progress === 100 || progress % 25 === 0) {
        void log(`bundle_progress=${progress}%`);
      }
    },
  });

  const composition = await selectComposition({
    serveUrl,
    id: "MATRIX-MOTION-Remotion-Phase1",
    browserExecutable: CHROMIUM,
    chromiumOptions: { enableMultiProcessOnLinux: true },
  });
  if (
    composition.width !== 1080 ||
    composition.height !== 1920 ||
    composition.fps !== FPS ||
    composition.durationInFrames !== EXPECTED_FRAMES
  ) {
    throw new Error(
      `La composición no coincide con la especificación: ${JSON.stringify({
        width: composition.width,
        height: composition.height,
        fps: composition.fps,
        durationInFrames: composition.durationInFrames,
      })}`,
    );
  }
  await log(
    `composition=${composition.width}x${composition.height} ` +
      `${composition.fps}fps ${composition.durationInFrames} frames`,
  );

  const resourceMonitor = await startResourceMonitor();
  const renderStarted = performance.now();
  await log("Inicio del único render autorizado; concurrencia=1.");
  let renderError = null;
  let renderMetrics;
  try {
    await renderMedia({
      composition,
      serveUrl,
      codec: "h264",
      outputLocation: outputPath,
      browserExecutable: CHROMIUM,
      chromiumOptions: { enableMultiProcessOnLinux: true },
      concurrency: 1,
      crf: 18,
      pixelFormat: "yuv420p",
      audioCodec: "aac",
      audioBitrate: "192k",
      logLevel: "warn",
    });
  } catch (error) {
    renderError = error;
  } finally {
    const renderDurationSeconds = (performance.now() - renderStarted) / 1000;
    const resourceMetrics = await resourceMonitor.stop();
    renderMetrics = { renderDurationSeconds, resourceMetrics };
    await log(`render_seconds=${renderDurationSeconds.toFixed(2)}`);
    await log(`resource_metrics=${JSON.stringify(resourceMetrics)}`);
  }
  if (renderError) {
    throw renderError;
  }
  await finishRender({
    outputPath,
    runDirectory: runDir,
    runId,
    originalStat,
    originalHashBefore,
    publicDir,
    bundleDir,
    renderMetrics,
  });
};

const finishRender = async ({
  outputPath,
  runDirectory,
  runId,
  originalStat,
  originalHashBefore,
  publicDir,
  bundleDir,
  renderMetrics,
}) => {
  const { renderDurationSeconds, resourceMetrics } = renderMetrics;

  const outputStat = await stat(outputPath);
  if (outputStat.size <= 0) {
    throw new Error("Remotion terminó sin generar un MP4 no vacío.");
  }
  const outputProbe = await getProbe(outputPath);
  const validation = validateOutput(outputProbe);
  const blackDetection = await run("ffmpeg", [
    "-hide_banner",
    "-loglevel",
    "info",
    "-i",
    outputPath,
    "-vf",
    "blackdetect=d=0.08:pix_th=0.10",
    "-an",
    "-f",
    "null",
    "-",
  ]);
  const blackEvents =
    (blackDetection.stderr.match(/black_start:/g) ?? []).length;
  const contactSheetPath = path.join(runDirectory, "contact-sheet.jpg");
  await run("ffmpeg", [
    "-nostdin",
    "-hide_banner",
    "-loglevel",
    "error",
    "-i",
    outputPath,
    "-vf",
    "fps=2,scale=270:480:force_original_aspect_ratio=decrease,tile=3x4",
    "-frames:v",
    "1",
    "-q:v",
    "2",
    "-y",
    contactSheetPath,
  ]);

  const originalHashAfter = await sha256File(INPUT_PATH);
  const audioSync = await verifyAudioAlignment({
    originalAudioPath: path.join(publicDir, "0415-audio.wav"),
    outputVideoPath: outputPath,
    runDirectory,
  });
  const originalUnchanged = originalHashBefore === originalHashAfter;
  const status =
    validation.valid &&
    originalUnchanged &&
    audioSync.status === "passed"
      ? "validated"
      : !validation.valid || !originalUnchanged || audioSync.status === "failed"
        ? "failed_validation"
        : "partial_validation";
  const report = {
    status,
    runId,
    input: {
      path: INPUT_PATH,
      bytes: originalStat.size,
      sha256Before: originalHashBefore,
      sha256After: originalHashAfter,
      originalUnchanged: originalHashBefore === originalHashAfter,
    },
    output: {
      path: outputPath,
      bytes: outputStat.size,
      probe: outputProbe,
      validation,
    },
    render: {
      width: 1080,
      height: 1920,
      fps: FPS,
      frames: EXPECTED_FRAMES,
      expectedDurationSeconds: EXPECTED_DURATION,
      renderDurationSeconds,
      concurrency: 1,
      codec: "h264",
      audioCodec: "aac",
      resourceMetrics,
      blackDetectionEvents: blackEvents,
      contactSheetPath,
      audioSourceMethod:
        "PCM temporal extraído del original; segmentos alineados por los mismos fotogramas fuente que el vídeo y fundidos en las dos transiciones.",
      audioSync,
      audioListeningCheck:
        "No se realizó escucha subjetiva; se conserva el MP4 para revisión.",
    },
    environment: {
      node: process.version,
      chromiumExecutable: CHROMIUM,
      telemetry: "licenseKey omitted; Remotion server-side telemetry not requested.",
      externalMediaSources: 0,
    },
  };
  await writeFile(
    path.join(runDirectory, "report.json"),
    `${JSON.stringify(report, null, 2)}\n`,
  );
  await log(`validation=${validation.valid ? "passed" : "failed"}`);
  await log(`black_detection_events=${blackEvents}`);
  await log(`audio_sync=${JSON.stringify(audioSync)}`);
  await log(`original_unchanged=${originalUnchanged}`);

  if (!validation.valid) {
    throw new Error(`FFprobe rechazó el resultado: ${validation.failures.join("; ")}`);
  }
  if (!originalUnchanged) {
    throw new Error("El SHA-256 del original cambió durante la operación.");
  }
  if (audioSync.status === "failed") {
    throw new Error(
      `La comprobación de sincronía de audio encontró un desfase de ${audioSync.lagMilliseconds} ms.`,
    );
  }

  await rm(publicDir, { recursive: true, force: true });
  await rm(bundleDir, { recursive: true, force: true });
  await log("Limpieza de la copia temporal y del bundle completada.");
  await log(`${status === "validated" ? "MP4_VALIDADO" : "MP4_PARCIAL"}=${outputPath}`);
  await log(`INFORME=${path.join(runDirectory, "report.json")}`);
};

main().catch(async (error) => {
  const message = error instanceof Error ? error.stack ?? error.message : String(error);
  try {
    await log(`ERROR: ${message}`);
    if (runDir) {
      await writeFile(
      path.join(runDir, "error.json"),
        `${JSON.stringify(
          { status: "failed", runId: path.basename(runDir), error: message },
          null,
          2,
        )}\n`,
      );
    }
  } catch (logError) {
    console.error("No se pudo guardar el diagnóstico:", logError);
  }
  console.error("Render detenido; no se realizará ningún reintento automático.");
  process.exitCode = 1;
});
