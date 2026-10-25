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
  resolveMasterRenderProfile,
  resolveRenderFrameCount,
  supportsAlphaOverlayPixelFormat,
} from "../src/master-render-profile.mjs";

test("master render profile supports source-appropriate vertical 4K through 60 FPS", () => {
  assert.deepEqual(resolveMasterRenderProfile(2160, 3840, 60), {
    width: 2160,
    height: 3840,
    fps: 60,
  });
  assert.deepEqual(resolveMasterRenderProfile(1080, 1920, 50), {
    width: 1080,
    height: 1920,
    fps: 50,
  });
  assert.throws(() => resolveMasterRenderProfile(1440, 2560, 60), /720p, 1080p o 2160p/u);
  assert.throws(() => resolveMasterRenderProfile(2160, 3840, 61), /25, 30, 50 o 60/u);
});

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

test("sound design maps one clean pop per caption/card and whooshes only to cuts", () => {
  assert.deepEqual(
    buildSoundEffectCues(clips, effects, words, effectCues),
    [
      { kind: "pop", startFrame: 3, durationFrames: 8 },
      { kind: "pop", startFrame: 40, durationFrames: 8 },
      { kind: "swoosh", startFrame: 90, durationFrames: 12 },
    ].sort((left, right) => left.startFrame - right.startFrame || left.kind.localeCompare(right.kind)),
  );
});

test("camera has no abrupt 8% hook and uses smoothstep capped at 1.06x", () => {
  const filter = buildZoompanFilter([5, 10, 120]);
  assert.doesNotMatch(filter, /1\.0800/u);
  assert.match(filter, /\*\(3-2\*/u);
  assert.match(filter, /1\.0600-/u);
  assert.match(filter, /between\(on\\,120\\,132\)/u);
  assert.doesNotMatch(filter, /between\(on\\,5\\,/u);
  assert.throws(() => buildZoompanFilter([-1]), /enteros no negativos/u);
  assert.throws(() => buildZoompanFilter([], { reframeScale: 1.08 }), /resolución/u);
});

test("every audited caption start gets a pop; shared frames are deduplicated", () => {
  const captionGroups = [{ start: 0.17 }, { start: 1.2 }, { start: 2.1 }];
  const result = buildSoundEffectCues([], [], [], [], 30, {
    captionGroups, overlays: [{ startFrame: 36 }],
  });
  assert.deepEqual(result.map((cue) => [cue.kind, cue.startFrame]),
    [["pop", 6], ["pop", 36], ["pop", 63]]);
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
  assert.match(graph, /\[0:v\]fps=30,select=/u);
  assert.ok(graph.includes("between(n\\,17\\,106)+between(n\\,120\\,199)"));
  assert.match(graph, /setpts=N\/\(30\*TB\)\[vcut\]/u);
  assert.doesNotMatch(graph, /split=2\[v/u);
  assert.match(graph, /\[vcut\]scale=1080:1920,format=yuv420p\[vcolor\]/u);
  assert.match(graph, /\[1:v\]format=rgba\[graphics\]/u);
  assert.match(graph, /\[0:2\]asplit=2/u);
  assert.match(graph, /\[1:a:0\]aresample=48000/u);
  assert.match(graph, /amix=inputs=2:duration=first:dropout_transition=0:normalize=0/u);
  assert.match(graph, /alimiter=limit=0\.95\[aout\]/u);
});

test("Remotion-composed video bypasses duplicate color, zoom and alpha-composite stages", () => {
  const graph = buildMasterFilterComplex({
    clips,
    sourceAudioStreamIndex: 2,
    colorFilter: "",
    jumpFrames: [],
    precomposedVideo: true,
  });
  assert.match(graph, /\[1:v\]fps=30,setpts=PTS-STARTPTS,format=yuv420p\[vout\]/u);
  assert.doesNotMatch(graph, /\[0:v\]|zoompan|overlay=0:0/u);
  assert.match(graph, /\[0:2\]asplit=2/u);
  assert.match(graph, /\[1:a:0\]aresample=48000/u);
});

test("product overlays trigger synchronized pops and restrained reframes", () => {
  const overlays = [{ startFrame: 130 }];
  assert.ok(buildJumpFrames(clips, words, [], 30, overlays).includes(130));
  assert.ok(buildSoundEffectCues(clips, [], words, [], 30, {
    includeCaptionEffects: false, overlays,
  }).some((cue) => cue.kind === "pop" && cue.startFrame === 130));
});

test("streaming video assembly rejects overlapping and reordered clips", () => {
  assert.throws(() => buildMasterFilterComplex({
    clips: [clips[0], { sourceStart: 50, frames: 20, outputStart: 90 }],
    sourceAudioStreamIndex: 1, colorFilter: "format=yuv420p", jumpFrames: [],
  }), /cronológicos/u);
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
