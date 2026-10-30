import { spawnSync } from "node:child_process";
import { existsSync, statSync, unlinkSync } from "node:fs";
import {
  APPROVED_MASTER_COLOR_PROFILE,
  REMOTION_RENDER_PROFILE,
  buildColorManagedProxyFilter,
} from "./render-profile.mjs";

const fail = (message) => {
  throw new Error(message);
};

const run = (command, args) => {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
    timeout: 6 * 60 * 60 * 1000,
  });
  if (result.error) fail(`${command} could not run: ${result.error.message}`);
  if (result.status !== 0) {
    const detail = String(result.stderr || result.stdout || "command failed")
      .trim()
      .split(/\r?\n/)
      .slice(-4)
      .join(" | ")
      .slice(0, 1000);
    fail(`${command} failed: ${detail}`);
  }
  return result.stdout || "";
};

const probe = (path) => {
  const data = JSON.parse(run("ffprobe", [
    "-v", "error",
    "-show_streams", "-show_format", "-of", "json", path,
  ]));
  const video = data.streams?.find((stream) => stream.codec_type === "video");
  if (!video) fail("La entrada no contiene una pista de vídeo.");
  const audios = (data.streams || []).filter((stream) => stream.codec_type === "audio");
  const duration = Number(data.format?.duration);
  if (!Number.isFinite(duration) || duration <= 0) fail("La entrada no tiene duración válida.");
  return { data, video, audios, duration };
};

const frameRate = (value) => {
  const [numerator, denominator] = String(value || "0/1").split("/").map(Number);
  return denominator ? numerator / denominator : 0;
};

const main = () => {
  const [inputPath, outputPath] = process.argv.slice(2);
  if (!inputPath || !outputPath || process.argv.length !== 4) {
    fail("Uso: normalize-input.mjs INPUT.mp4|mov OUTPUT.mp4");
  }
  if (inputPath === outputPath || existsSync(outputPath)) {
    fail("La normalización requiere una salida nueva; no se sobrescriben archivos.");
  }
  const source = probe(inputPath);
  const selectedAudio = source.audios.find(
    (stream) => !["apac", "unknown"].includes(String(stream.codec_name || "").toLowerCase()),
  );
  if (source.audios.length && !selectedAudio) {
    fail("No se encontró una pista de audio compatible; no se sustituirá el audio original por silencio.");
  }
  const filter = buildColorManagedProxyFilter(
    source.video,
    REMOTION_RENDER_PROFILE.fps,
    {
      ...APPROVED_MASTER_COLOR_PROFILE,
      width: REMOTION_RENDER_PROFILE.width,
      height: REMOTION_RENDER_PROFILE.height,
    },
  );
  const args = [
    "-nostdin", "-n", "-hide_banner", "-loglevel", "error",
    "-threads", "2", "-filter_threads", "1",
    "-i", inputPath,
  ];
  if (!selectedAudio) {
    args.push(
      "-f", "lavfi", "-t", String(source.duration),
      "-i", "anullsrc=r=48000:cl=stereo",
    );
  }
  args.push("-map", `0:${source.video.index}`);
  if (selectedAudio) args.push("-map", `0:${selectedAudio.index}`);
  else args.push("-map", "1:a:0");
  args.push(
    "-vf", filter,
    // The fps=30 filter sets constant frame timestamps and is supported by FFmpeg 4.4.
    // Avoid -fps_mode, which is unavailable in the production FFmpeg 4.4.2 build.
    "-c:v", "libx264", "-preset", "medium", "-crf", "17",
    "-profile:v", "high", "-pix_fmt", REMOTION_RENDER_PROFILE.pixelFormat,
    "-threads", "2",
    "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
    "-map_metadata", "-1", "-map_chapters", "-1",
    "-color_range", "tv", "-colorspace", "bt709",
    "-color_primaries", "bt709", "-color_trc", "bt709",
    "-movflags", "+faststart",
  );
  run("ffmpeg", [...args, outputPath]);
  try {
    const output = probe(outputPath);
    const video = output.video;
    const audio = output.audios[0];
    if (
      video.codec_name !== "h264" ||
      video.width !== REMOTION_RENDER_PROFILE.width ||
      video.height !== REMOTION_RENDER_PROFILE.height ||
      video.pix_fmt !== "yuv420p" ||
      Math.abs(frameRate(video.avg_frame_rate || video.r_frame_rate) - 30) > 0.05 ||
      video.color_primaries !== "bt709" ||
      video.color_transfer !== "bt709" ||
      video.color_space !== "bt709" ||
      video.color_range !== "tv" ||
      !audio ||
      audio.codec_name !== "aac" ||
      Number(audio.sample_rate) !== 48000 ||
      Number(audio.channels) !== 2 ||
      Math.abs(output.duration - source.duration) > Math.max(0.1, 1 / 30)
    ) {
      fail("FFprobe rechazó el perfil generado; se eliminará la copia temporal.");
    }
    console.log(JSON.stringify({
      source_color_profile: {
        color_range: source.video.color_range,
        color_primaries: source.video.color_primaries,
        color_transfer: source.video.color_transfer,
        color_space: source.video.color_space,
      },
      audio_codec: audio.codec_name,
      audio_sample_rate: Number(audio.sample_rate),
      audio_channels: Number(audio.channels),
      output_size_bytes: statSync(outputPath).size,
    }));
  } catch (error) {
    unlinkSync(outputPath);
    throw error;
  }
};

try {
  main();
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
