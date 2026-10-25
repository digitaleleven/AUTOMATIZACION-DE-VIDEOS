import test from "node:test";
import assert from "node:assert/strict";
import {
  activeCaptionGroup,
  activeWordIndex,
  groupTimedWords,
  isKeywordWord,
  keywordWindowsForSegment,
  mapWordTimingsToOutput,
  parseWhisperWordTimings,
} from "../src/caption-timing.mjs";
import {
  DURATION_IN_FRAMES,
  FPS,
  SEGMENTS,
  getSourceClipWindow,
} from "../src/edit-timeline.mjs";
import {
  buildColorManagedProxyFilter,
  hasVisualContent,
  parseAllIntraFrameInfo,
  REMOTION_RENDER_PROFILE,
} from "../src/render-profile.mjs";

test("Whisper parser requires precise, positive word intervals", () => {
  const words = parseWhisperWordTimings({
    words: [
      { word: "Hola", start: 0.2, end: 0.5 },
      { word: "bad", start: 1, end: 1 },
      { word: " mundo.", start: 0.5, end: 0.9 },
    ],
  });

  assert.deepEqual(words.map(({ text }) => text), ["Hola", "mundo."]);
  assert.throws(() => parseWhisperWordTimings({ segments: [{ text: "sin palabras" }] }), /marcas temporales por palabra/);
});

test("only complete word timings inside kept clips are mapped to output time", () => {
  const words = [
    { text: "Hola", start: 0.2, end: 0.5 },
    { text: "chicas", start: 1.6, end: 2.1 },
    { text: "corte", start: 4.9, end: 5.1 },
    { text: "¿Qué", start: 5.1, end: 5.3 },
  ];
  const mapped = mapWordTimingsToOutput(words, SEGMENTS, FPS);

  assert.equal(mapped.length, 2);
  assert.deepEqual(mapped.map(({ text }) => text), ["Hola", "¿Qué"]);
  assert.ok(Math.abs(mapped[0].start - 0.2) < 1e-9);
  assert.ok(Math.abs(mapped[0].end - 0.5) < 1e-9);
  assert.ok(Math.abs(mapped[1].start - 1.7) < 1e-9);
  assert.ok(Math.abs(mapped[1].end - 1.9) < 1e-9);
  assert.equal(DURATION_IN_FRAMES, 171);
});

test("Remotion source clips use frame-based trim and duration props", () => {
  const windows = SEGMENTS.map(({ sourceStart, frames }) =>
    getSourceClipWindow(sourceStart, frames, 600),
  );

  assert.deepEqual(windows, [
    { trimBefore: 0, durationInFrames: 60 },
    { trimBefore: 150, durationInFrames: 75 },
    { trimBefore: 300, durationInFrames: 60 },
  ]);
  assert.throws(() => getSourceClipWindow(300, 100, 360), /Invalid source clip frame range/);
});

test("caption groups stay within one to three words and highlight one timed word", () => {
  const words = [
    { text: "Una", start: 0, end: 0.2 },
    { text: "prueba", start: 0.2, end: 0.5 },
    { text: "con", start: 0.5, end: 0.7 },
    { text: "palabras", start: 0.7, end: 1.0 },
    { text: "sin", start: 1, end: 1.2 },
    { text: "inventar.", start: 1.2, end: 1.6 },
  ];
  const groups = groupTimedWords(words);

  assert.deepEqual(groups.map((group) => group.words.length), [3, 3]);
  const group = activeCaptionGroup(groups, 0.82);
  assert.ok(group);
  assert.equal(activeWordIndex(group, 0.82), 0);
  assert.equal(activeWordIndex(group, 1.0), 1);
  assert.equal(activeCaptionGroup(groups, 2), null);
});

test("overlapping transitions choose only the most recently started group and word", () => {
  const earlierGroup = {
    start: 1,
    end: 2,
    words: [{ text: "sale", start: 1, end: 1.6 }],
  };
  const incomingGroup = {
    start: 1.4,
    end: 2.2,
    words: [{ text: "entra", start: 1.4, end: 1.8 }],
  };
  const active = activeCaptionGroup([earlierGroup, incomingGroup], 1.5);

  assert.equal(active, incomingGroup);
  assert.equal(activeWordIndex({
    words: [
      { text: "antes", start: 1.0, end: 1.6 },
      { text: "ahora", start: 1.4, end: 1.8 },
    ],
  }, 1.5), 1);
});

test("keyword zoom windows skip Spanish stopwords and use segment-local frames", () => {
  const words = [
    { text: "que", start: 1.6, end: 1.8 },
    { text: "me", start: 1.8, end: 1.9 },
    { text: "está", start: 1.9, end: 2.0 },
    { text: "funcionando", start: 2.0, end: 2.4 },
    { text: "perfectamente", start: 3.9, end: 4.0 },
  ];

  assert.equal(isKeywordWord("está"), false);
  assert.equal(isKeywordWord("funcionando"), true);
  assert.deepEqual(
    keywordWindowsForSegment(words, { outputStart: 48, frames: 75 }, 30),
    [
      { startFrame: 12, endFrame: 24 },
      { startFrame: 69, endFrame: 72 },
    ],
  );
});

test("HLG proxy conversion is explicit and outputs limited BT.709 YUV420p", () => {
  const filter = buildColorManagedProxyFilter({
    color_range: "tv",
    color_space: "bt2020nc",
    color_transfer: "arib-std-b67",
    color_primaries: "bt2020",
  });

  assert.match(filter, /transferin=arib-std-b67/);
  assert.match(filter, /tonemap=tonemap=hable:desat=0/);
  assert.match(filter, /primaries=bt709:transfer=bt709:matrix=bt709:range=limited/);
  assert.match(filter, /format=yuv420p/);
  assert.match(buildColorManagedProxyFilter({
    color_range: "tv",
    color_space: "bt2020nc",
    color_transfer: "arib-std-b67",
    color_primaries: "bt2020",
  }, 30), /fps=30/);
  assert.equal(REMOTION_RENDER_PROFILE.colorSpace, "bt709");
  assert.equal(REMOTION_RENDER_PROFILE.concurrency, 1);
  assert.equal(REMOTION_RENDER_PROFILE.maxRetries, 0);
});

test("Mobius is applied in linear RGB before BT.709 and outputs a padded 1080p proxy", () => {
  const filter = buildColorManagedProxyFilter({
    color_range: "tv",
    color_space: "bt2020nc",
    color_transfer: "arib-std-b67",
    color_primaries: "bt2020",
  }, 30, { toneMap: "mobius", mobiusParam: 0.5 });

  const linearRgb = filter.indexOf("format=gbrpf32le");
  const mobius = filter.indexOf("tonemap=tonemap=mobius:param=0.5");
  const bt709 = filter.indexOf("primaries=bt709:transfer=bt709");
  assert.ok(linearRgb >= 0 && linearRgb < mobius && mobius < bt709);
  assert.match(filter, /force_original_aspect_ratio=decrease:flags=lanczos/);
  assert.match(filter, /pad=1080:1920:\(ow-iw\)\/2:\(oh-ih\)\/2/);
  assert.match(filter, /fps=30,format=yuv420p/);
  assert.throws(
    () => buildColorManagedProxyFilter({
      color_range: "tv",
      color_space: "bt2020nc",
      color_transfer: "arib-std-b67",
      color_primaries: "bt2020",
    }, 30, { toneMap: "mobius", mobiusParam: 0 }),
    /El parámetro Mobius debe ser positivo/,
  );
});

test("direct HLG conversion bypasses tone mapping but converts to BT.709 before EQ", () => {
  const filter = buildColorManagedProxyFilter({
    color_range: "tv",
    color_space: "bt2020nc",
    color_transfer: "arib-std-b67",
    color_primaries: "bt2020",
  }, 30, { toneMap: "direct" });

  const fps = filter.indexOf("fps=30");
  const scale = filter.indexOf("scale=1080:1920");
  const unsharp = filter.indexOf("unsharp=5:5:0.8:5:5:0.0");
  const zscale = filter.indexOf("zscale=primariesin=bt2020");
  const eq = filter.indexOf("eq=contrast=1.05:brightness=0.04:gamma=1.15:saturation=0.92");
  const channelMix = filter.indexOf("colorchannelmixer=rr=0.90:gg=0.95:bb=1.10");
  const format = filter.indexOf("format=yuv420p");
  assert.ok(fps >= 0 && fps < scale && scale < unsharp && unsharp < zscale && zscale < eq && eq < channelMix && channelMix < format);
  assert.match(filter, /primaries=bt709:transfer=bt709:matrix=bt709:range=limited:npl=1000/);
  assert.match(filter, /pad=1080:1920:\(ow-iw\)\/2:\(oh-ih\)\/2/);
  assert.match(filter, /format=yuv420p/);
  assert.doesNotMatch(filter, /tonemap=|gbrpf32le/);
});

test("unsupported or incomplete source color metadata fails explicitly", () => {
  assert.throws(
    () => buildColorManagedProxyFilter({
      color_range: "tv",
      color_space: "bt2020nc",
      color_transfer: "smpte2084",
      color_primaries: "bt2020",
    }),
    /Perfil de entrada no admitido/,
  );
  assert.throws(
    () => buildColorManagedProxyFilter({
      color_range: "unknown",
      color_space: "bt709",
      color_transfer: "bt709",
      color_primaries: "bt709",
    }),
    /rango de color explícito/,
  );
});

test("visual signal validation rejects black renders", () => {
  assert.equal(hasVisualContent([18, 19, 20, 22, 21, 20]), false);
  assert.equal(hasVisualContent([18, 120, 160, 140, 22, 130]), true);
  assert.equal(hasVisualContent([128, 160]), false);
});

test("all-intra verification requires every probed frame to be an I-frame", () => {
  assert.deepEqual(parseAllIntraFrameInfo("1,I\n1,I\n1,I\n"), {
    frameCount: 3,
    allIntra: true,
  });
  assert.deepEqual(parseAllIntraFrameInfo("1,I\n0,P\n"), {
    frameCount: 2,
    allIntra: false,
  });
});
