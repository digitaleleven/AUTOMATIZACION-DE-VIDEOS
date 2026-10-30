export const MASTER_CLI_USAGE =
  "Uso: render-master.mjs [--input MOV] [--transcript JSON] [--plan JSON] " +
  "[--effects-dir DIR] [--output MP4] [--show-subtitles true|false] " +
  "[--render-frames COUNT] [--briefing TEXT] [--caption-style-json JSON] " +
  "[--width PX] [--height PX] [--fps FPS] [--normalized-video MP4]";

const ALLOWED_OPTIONS = new Set([
  "--input", "--transcript", "--plan", "--effects-dir", "--output",
  "--show-subtitles", "--render-frames", "--briefing", "--caption-style-json",
  "--width", "--height", "--fps", "--normalized-video",
]);

/** Parse argument pairs without invoking media, Chromium or filesystem operations. */
export function parseMasterOptionPairs(args) {
  const parsed = {};
  for (let index = 0; index < args.length; index += 2) {
    const key = args[index];
    const value = args[index + 1];
    if (
      !ALLOWED_OPTIONS.has(key) ||
      typeof value !== "string" ||
      value.startsWith("--") ||
      (value === "" && key !== "--briefing")
    ) {
      throw new Error(MASTER_CLI_USAGE);
    }
    parsed[key] = value;
  }
  return parsed;
}
