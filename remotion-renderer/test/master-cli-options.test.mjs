import assert from "node:assert/strict";
import test from "node:test";
import { parseMasterOptionPairs } from "../src/master-cli-options.mjs";

test("accepts the worker's real 4K/60 argument pattern with an empty optional briefing", () => {
  const parsed = parseMasterOptionPairs([
    "--input", "/tmp/source.mp4",
    "--transcript", "/tmp/transcript.json",
    "--plan", "/tmp/plan.json",
    "--effects-dir", "/tmp/effects",
    "--output", "/tmp/rendered.mp4",
    "--show-subtitles", "true",
    "--briefing", "",
    "--caption-style-json", '{"highlight_color":"#FFC0CB"}',
    "--width", "2160", "--height", "3840", "--fps", "60",
    "--normalized-video", "/tmp/proxy-bt709.mp4",
  ]);
  assert.equal(parsed["--briefing"], "");
  assert.equal(parsed["--width"], "2160");
  assert.equal(parsed["--fps"], "60");
  assert.equal(parsed["--normalized-video"], "/tmp/proxy-bt709.mp4");
});

test("preserves nonempty briefing text without interpreting it as shell input", () => {
  assert.equal(
    parseMasterOptionPairs(["--briefing", "Texto con espacios; $(no ejecutar)"])["--briefing"],
    "Texto con espacios; $(no ejecutar)",
  );
});

test("still rejects missing values, empty paths, option-as-value and unknown flags", () => {
  for (const args of [
    ["--briefing"], ["--input", ""], ["--width", ""],
    ["--input", "--output"], ["--not-supported", "true"],
  ]) {
    assert.throws(() => parseMasterOptionPairs(args), /Uso: render-master/u);
  }
});
