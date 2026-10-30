import assert from "node:assert/strict";
import test from "node:test";
import {
  buildKeepSegments,
  buildTimedVectorEffects,
  cameraScaleAtFrame,
  isSceneCutBlurFrame,
  FULL_FPS,
  FULL_HEIGHT,
  FULL_WIDTH,
} from "../src/full-edit-timeline.mjs";

const cuts = [
  { start_seconds: 0, end_seconds: 0.55 },
  { start_seconds: 33.72, end_seconds: 34.35 },
  { start_seconds: 36.54, end_seconds: 37.07 },
];

test("legacy full-edit timeline keeps its separate camera helper neutral", () => {
  assert.equal(FULL_WIDTH, 1080);
  assert.equal(FULL_HEIGHT, 1920);
  for (const frame of [0, 1, 30, 908, 1496, 1746]) {
    assert.equal(cameraScaleAtFrame(frame), 1);
  }
});

test("master camera zoom stays smooth and Remotion blur is confined to cut boundaries", () => {
  assert.equal(cameraScaleAtFrame(20, [20]), 1);
  assert.equal(cameraScaleAtFrame(26, [20]), 1.03);
  assert.equal(cameraScaleAtFrame(32, [20]), 1.06);
  assert.equal(cameraScaleAtFrame(44, [20]), 1);
  assert.equal(cameraScaleAtFrame(30, [], 12, 1.06, [{ startFrame: 30, endFrame: 90 }]), 1);
  assert.equal(cameraScaleAtFrame(36, [], 12, 1.06, [{ startFrame: 30, endFrame: 90 }]), 1.03);
  assert.equal(cameraScaleAtFrame(50, [], 12, 1.06, [{ startFrame: 30, endFrame: 90 }]), 1.06);
  assert.equal(cameraScaleAtFrame(84, [], 12, 1.06, [{ startFrame: 30, endFrame: 90 }]), 1.03);
  assert.equal(cameraScaleAtFrame(90, [], 12, 1.06, [{ startFrame: 30, endFrame: 90 }]), 1);
  assert.equal(cameraScaleAtFrame(44, [], 12, 1.06, [{
    startFrame: 50, endFrame: 100, effect: "zoomOut",
  }]), 1.03);
  assert.equal(cameraScaleAtFrame(56, [], 12, 1.06, [{
    startFrame: 50, endFrame: 100, effect: "zoomOut",
  }]), 1.03);
  assert.equal(cameraScaleAtFrame(62, [], 12, 1.06, [{
    startFrame: 50, endFrame: 100, effect: "zoomOut",
  }]), 1);
  assert.equal(isSceneCutBlurFrame(89, [90]), false);
  assert.equal(isSceneCutBlurFrame(90, [90]), true);
});

test("MM-004 cuts preserve all 1799 proxy frames outside cuts on a gapless timeline", () => {
  const clips = buildKeepSegments(1799, cuts);
  assert.deepEqual(clips, [
    { sourceStart: 17, frames: 995, outputStart: 0 },
    { sourceStart: 1031, frames: 65, outputStart: 995 },
    { sourceStart: 1112, frames: 687, outputStart: 1060 },
  ]);
  assert.equal(clips.reduce((sum, clip) => sum + clip.frames, 0), 1747);
});

test("vector overlays last 1.5–2 seconds and clamp at the final frame", () => {
  const cues = [
    { effect: "droplet", start_seconds: 30.27, assets: ["water-drop"], position: "topRight" },
    { effect: "pearl", start_seconds: 49.85, assets: ["pearl-glints"], position: "topRight" },
    { effect: "kiss", start_seconds: 56.51, assets: ["kiss-lips", "kiss-heart"], position: "topRight" },
  ];
  const effects = buildTimedVectorEffects(cues, 1747, FULL_FPS);
  assert.deepEqual(effects.map(({ startFrame, durationFrames }) => [startFrame, durationFrames]), [
    [908, 54],
    [1496, 54],
    [1695, 52],
  ]);
  assert.ok(effects.every((effect) => effect.durationFrames / FULL_FPS >= 1.5));
});

test("overlapping cuts and effects without enough timeline are rejected", () => {
  assert.throws(
    () => buildKeepSegments(1800, [
      { start_seconds: 1, end_seconds: 2 },
      { start_seconds: 1.5, end_seconds: 3 },
    ]),
    /inválidos, solapados/u,
  );
  assert.throws(
    () => buildTimedVectorEffects(
      [{ effect: "kiss", start_seconds: 58.1, assets: ["kiss-lips"] }],
      1748,
      FULL_FPS,
    ),
    /no cabe/u,
  );
});
