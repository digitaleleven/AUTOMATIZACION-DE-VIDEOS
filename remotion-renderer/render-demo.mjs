import { spawn } from "node:child_process";
import { mkdir, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";

const packageDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(packageDir, "..");
const input = path.join(root, "outputs/IMG_1254-MATRIX-MOTION-v3-PRO-MASTER.mp4");
const output = path.join(
  root,
  "generated-artifacts/drive-inspection/remotion/demo/IMG_1254-master-demo-720p.mp4",
);

const run = (command, args) =>
  new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: "inherit" });
    child.on("error", reject);
    child.on("close", (code, signal) => {
      if (code === 0) resolve();
      else reject(new Error(`${command} finalizó con código ${code ?? "null"}${signal ? ` (${signal})` : ""}`));
    });
  });

await mkdir(path.dirname(output), { recursive: true });
const inputInfo = await stat(input).catch(() => null);
if (!inputInfo?.isFile() || inputInfo.size === 0) {
  throw new Error(`No existe el máster local de entrada: ${input}`);
}
if (await stat(output).then(() => true).catch(() => false)) {
  throw new Error(`No se sobrescribe la muestra demo existente: ${output}`);
}

const started = performance.now();
await run("ffmpeg", [
  "-nostdin", "-n", "-hide_banner", "-loglevel", "error",
  "-i", input, "-t", "10",
  "-vf", "scale=720:1280:force_original_aspect_ratio=decrease:flags=lanczos,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1",
  "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
  "-profile:v", "high", "-pix_fmt", "yuv420p", "-fps_mode", "cfr",
  "-color_range", "tv", "-colorspace", "bt709",
  "-color_primaries", "bt709", "-color_trc", "bt709",
  "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
  "-movflags", "+faststart", output,
]);
const elapsed = (performance.now() - started) / 1000;
const outputInfo = await stat(output);
console.log(`demo_output=${output}`);
console.log(`demo_seconds=${elapsed.toFixed(2)}`);
console.log(`demo_size_bytes=${outputInfo.size}`);
