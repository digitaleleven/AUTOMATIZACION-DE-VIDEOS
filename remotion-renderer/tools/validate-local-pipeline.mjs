// Opt-in integration check. Renders only 60 frames; never called by unit tests.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { runLocalProcess } from "../src/local-process.mjs";
import { buildKeepSegments } from "../src/full-edit-timeline.mjs";
import { mapWordTimingsToOutput, parseWhisperWordTimings } from "../src/caption-timing.mjs";

const packageDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const root = path.resolve(packageDir, "..");
const source = path.join(root, "generated-artifacts/drive-inspection/IMG_1254.MOV");
const transcriptPath = path.join(root, "generated-artifacts/drive-inspection/q03a/transcription-corrected.json");
const plan = JSON.parse(await readFile(path.join(root,
  "generated-artifacts/drive-inspection/mm004/mm004-plan.json"), "utf8"));
const transcript = JSON.parse(await readFile(transcriptPath, "utf8"));
const sourceProbe = JSON.parse((await runLocalProcess("ffprobe", [
  "-v", "error", "-show_format", "-of", "json", source,
])).stdout);
const clips = buildKeepSegments(Math.round(Number(sourceProbe.format.duration) * 30), plan.cuts, 30);
const words = mapWordTimingsToOutput(parseWhisperWordTimings(transcript), clips, 30);
assert.ok(words[0]?.start < 2, "The test requires an aligned spoken word within the preview.");
const workRoot = path.join(root, "generated-artifacts/pipeline-validation");
await mkdir(workRoot, { recursive: true });
const work = await mkdtemp(path.join(workRoot, "run-"));
const effectsDir = path.join(root, "generated-artifacts/drive-inspection/mm004/effects");
// A neutral local fixture, explicitly labelled as a test, not a product image.
await runLocalProcess("ffmpeg", [
  "-nostdin", "-n", "-v", "error", "-f", "lavfi",
  "-i", "color=c=0xFF2A85:s=320x320", "-frames:v", "1",
  "-threads", "1", path.join(work, "fixture.png"),
]);
const { copyFile, readdir } = await import("node:fs/promises");
for (const name of await readdir(effectsDir)) {
  if (name.endsWith(".svg")) await copyFile(path.join(effectsDir, name), path.join(work, name));
}
plan.overlays = {
  title: { text: "Prueba local de overlays", duration_seconds: 2 },
  promo: "VALIDACIÓN TÉCNICA",
  images: [{
    asset: "fixture.png", label: "Recurso de prueba",
    trigger_text: words[0].text, position: "right",
  }],
  cards: [{
    text: "Datos explícitos", start_seconds: 0.5, position: "topLeft",
  }],
};
const planPath = path.join(work, "validation-plan.json");
await writeFile(planPath, JSON.stringify(plan, null, 2));
const output = path.join(work, "local-overlays-2s.mp4");
const started = performance.now();
try {
  const result = await runLocalProcess(process.execPath, [
    path.join(packageDir, "render-master.mjs"),
    "--input", source, "--plan", planPath, "--transcript", transcriptPath,
    "--effects-dir", work, "--output", output, "--render-frames", "60",
    "--show-subtitles", "true",
  ], { cwd: packageDir, timeoutMs: 10 * 60 * 1000 });
  await writeFile(path.join(work, "render.log"), result.stdout + "\n" + result.stderr);
  const report = JSON.parse(await readFile(output.replace(".mp4", ".report.json"), "utf8"));
  assert.equal(report.output.fullDecode, "passed");
  assert.equal(report.output.sourceHashUnchanged, true);
  assert.equal(report.output.frames, 60);
  const overlays = report.pipeline?.stage1?.creativeOverlays ?? report.stages?.stage1?.creativeOverlays;
  assert.equal(overlays.title.text, "Prueba local de overlays");
  assert.equal(overlays.promo.text, "VALIDACIÓN TÉCNICA");
  assert.equal(overlays.images.length, 1);
  assert.equal(overlays.cards.length, 1);
  // Independent video/audio probe in addition to the renderer's validation.
  const probe = JSON.parse((await runLocalProcess("ffprobe", [
    "-v", "error", "-show_streams", "-show_format", "-of", "json", output,
  ])).stdout);
  assert.ok(probe.streams.some((stream) => stream.codec_name === "h264" && stream.width === 1080 && stream.height === 1920));
  assert.ok(probe.streams.some((stream) => stream.codec_name === "aac" && stream.sample_rate === "48000"));
  await runLocalProcess("ffmpeg", [
    "-nostdin", "-n", "-v", "error", "-ss", "1", "-i", output,
    "-frames:v", "1", "-threads", "1", path.join(work, "overlays-frame.png"),
  ]);
  console.log(JSON.stringify({
    output, report: output.replace(".mp4", ".report.json"),
    wallSeconds: (performance.now() - started) / 1000,
    frames: 60, sourceBytes: Number(sourceProbe.format.size), overlays,
    fullDecode: "passed", sourceUnchanged: true,
  }, null, 2));
} catch (error) {
  await writeFile(path.join(work, "validation-error.log"),
    `${error.stack}\n${error.stderr ?? ""}\n${error.stdout ?? ""}`);
  console.error(`Prueba detenida sin reintentar. Diagnóstico: ${work}/validation-error.log`);
  throw error;
}
