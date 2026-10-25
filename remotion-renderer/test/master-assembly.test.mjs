import assert from "node:assert/strict";
import test from "node:test";
import {
  buildNaturalReframeFrames,
  buildJumpFrames,
  buildMasterFilterComplex,
  buildSoundEffectCues,
  buildZoompanFilter,
} from "../src/master-assembly.mjs";
import {
  MASTER_OVERLAY_PROFILE,
  resolveRenderFrameCount,
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

test("reframe events follow natural speech pauses and cuts, not each word", () => {
  assert.deepEqual(buildNaturalReframeFrames(words), [3, 36]);
  assert.deepEqual(buildJumpFrames(clips, words, effectCues), [3, 36, 90]);
});

test("sound design maps a low impact hook, subtle word pops, graphics, and transitions", () => {
  assert.deepEqual(
    buildSoundEffectCues(clips, effects, words, effectCues),
    [
      { kind: "impact", startFrame: 3, durationFrames: 18 },
      { kind: "swoosh", startFrame: 3, durationFrames: 12 },
      { kind: "pop", startFrame: 36, durationFrames: 8 },
      { kind: "ding", startFrame: 36, durationFrames: 10 },
      { kind: "pop", startFrame: 40, durationFrames: 8 },
      { kind: "swoosh", startFrame: 90, durationFrames: 12 },
    ].sort((left, right) => left.startFrame - right.startFrame || left.kind.localeCompare(right.kind)),
  );
});

test("camera motion eases out an 8% hook and eases between phrases to 1.06x", () => {
  const filter = buildZoompanFilter([5, 10, 120]);
  assert.match(filter, /1\.0800/u);
  assert.match(filter, /0\.0800\*\(on-90\)\/12/u);
  assert.match(filter, /1\.0600-/u);
  assert.match(filter, /between\(on\\,120\\,132\)/u);
  assert.doesNotMatch(filter, /between\(on\\,5\\,/u);
  assert.throws(() => buildZoompanFilter([-1]), /enteros no negativos/u);
});

test("caption-linked sound effects stay off when showSubtitles is disabled", () => {
  assert.deepEqual(
    buildSoundEffectCues(clips, [], words, [], 30, { includeCaptionEffects: false }),
    [{ kind: "swoosh", startFrame: 90, durationFrames: 12 }],
  );
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

test("preview renders validate frame range without changing the full master default", () => {
  assert.equal(resolveRenderFrameCount(undefined, 1747), 1747);
  assert.equal(resolveRenderFrameCount("300", 1747), 300);
  assert.throws(() => resolveRenderFrameCount("0", 1747), /entre 1 y 1747/u);
  assert.throws(() => resolveRenderFrameCount("1748", 1747), /entre 1 y 1747/u);
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

test("preview without timed effects preserves source audio without requiring an overlay audio stream", () => {
  const graph = buildMasterFilterComplex({
    clips: [clips[0]],
    sourceAudioStreamIndex: 2,
    colorFilter: "fps=30,format=yuv420p",
    jumpFrames: [],
    includeSoundEffects: false,
  });
  assert.match(graph, /\[aoriginal\]anull\[aout\]/u);
  assert.doesNotMatch(graph, /\[1:a:0\]/u);
});
