import assert from "node:assert/strict";
import test from "node:test";
import {
  buildJumpFrames,
  buildMasterFilterComplex,
  buildSoundEffectCues,
  buildZoompanFilter,
} from "../src/master-assembly.mjs";
import {
  MASTER_OVERLAY_PROFILE,
  supportsAlphaOverlayPixelFormat,
} from "../src/master-render-profile.mjs";

const clips = [
  { sourceStart: 17, frames: 90, outputStart: 0 },
  { sourceStart: 120, frames: 80, outputStart: 90 },
];
const words = [
  { text: "¡Hola", start: 0.1, end: 0.3 },
  { text: "tónico.", start: 1.2, end: 1.5 },
];
const effects = [{ id: "drop", startFrame: 40, durationFrames: 54 }];
const effectCues = [{ trigger_word: "tonico" }];

test("one-frame emphasis uses only cut boundaries and timed hook words", () => {
  assert.deepEqual(buildJumpFrames(clips, words, effectCues), [36, 90]);
});

test("sound design maps swoosh, sticker pop, and hook ding to the edited timeline", () => {
  assert.deepEqual(
    buildSoundEffectCues(clips, effects, words, effectCues),
    [
      { kind: "pop", startFrame: 40, durationFrames: 8 },
      { kind: "ding", startFrame: 36, durationFrames: 10 },
      { kind: "swoosh", startFrame: 90, durationFrames: 12 },
    ].sort((left, right) => left.startFrame - right.startFrame || left.kind.localeCompare(right.kind)),
  );
});

test("zoom is a single frame at 1.06x, not progressive camera movement", () => {
  assert.equal(buildZoompanFilter([5, 10]), "zoompan=z='if(eq(on\\,5)+eq(on\\,10)\\,1.06\\,1)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps=30");
  assert.equal(buildZoompanFilter([]), "null");
  assert.throws(() => buildZoompanFilter([-1]), /enteros no negativos/u);
});

test("transparent Remotion overlay uses PNG frames with ProRes 4444 alpha and no retries", () => {
  assert.deepEqual(MASTER_OVERLAY_PROFILE, {
    codec: "prores",
    proResProfile: "4444",
    pixelFormat: "yuva444p10le",
    imageFormat: "png",
    audioCodec: "pcm-16",
    sampleRate: 48000,
    concurrency: 1,
    maxRetries: 0,
    disallowParallelEncoding: true,
  });
  assert.equal(supportsAlphaOverlayPixelFormat("yuva444p10le"), true);
  assert.equal(supportsAlphaOverlayPixelFormat("yuva444p12le"), true);
  assert.equal(supportsAlphaOverlayPixelFormat("yuv420p"), false);
});

test("FFmpeg graph trims source video and audio, overlays alpha graphics, and mixes SFX", () => {
  const graph = buildMasterFilterComplex({
    clips,
    sourceAudioStreamIndex: 2,
    colorFilter: "fps=30,scale=1080:1920,format=yuv420p",
    jumpFrames: [36, 90],
  });
  assert.match(graph, /\[0:v\]fps=30,split=2/u);
  assert.match(graph, /trim=start_frame=17:end_frame=107/u);
  assert.match(graph, /trim=start_frame=120:end_frame=200/u);
  assert.match(graph, /concat=n=2:v=1:a=0\[vcut\]/u);
  assert.match(graph, /\[vcut\]scale=1080:1920,format=yuv420p\[vcolor\]/u);
  assert.match(graph, /\[1:v\]format=rgba\[graphics\]/u);
  assert.match(graph, /\[0:2\]asplit=2/u);
  assert.match(graph, /\[1:a:0\]aresample=48000/u);
  assert.match(graph, /amix=inputs=2:duration=first:dropout_transition=0:normalize=0/u);
  assert.match(graph, /alimiter=limit=0\.95\[aout\]/u);
});
