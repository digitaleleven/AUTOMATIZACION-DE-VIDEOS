import assert from "node:assert/strict";
import test from "node:test";
import { prepareCaptionTrack } from "../src/caption-coverage.mjs";
import { activeWordIndex } from "../src/caption-timing.mjs";

const clips = [{ sourceStart: 0, frames: 300, outputStart: 0 }];
test("zero duration words are displayed with their anchor, not silently dropped or falsely highlighted", () => {
  const track = prepareCaptionTrack({
    text: "Qué tal Hailey Bieber",
    words: [
      { word: "Qué", start: 1, end: 1 },
      { word: "tal", start: 1, end: 1.4 },
      { word: "Hailey Bieber", start: 2, end: 2.6 },
    ],
  }, clips);
  assert.equal(track.audit.displayedWords, 4);
  assert.equal(track.audit.omittedWords, 0);
  assert.equal(track.audit.sharedWindows, 2);
  assert.equal(activeWordIndex(track.groups[0], 1.2), -1);
  assert.equal(activeWordIndex(track.groups[1], 2.2), -1);
});
test("text mismatch, cut words and unanchored words block rendering", () => {
  const word = { word: "Hola", start: 1, end: 1.5 };
  assert.throws(() => prepareCaptionTrack({ text: "Hola mundo", words: [word] }, clips), /no coinciden/u);
  assert.throws(() => prepareCaptionTrack({ text: "Hola", words: [word] }, [
    { sourceStart: 40, frames: 40, outputStart: 0 },
  ]), /corte/u);
  assert.throws(() => prepareCaptionTrack({ text: "Hola", words: [{ ...word, end: 1 }] }, clips), /ancla/u);
});
test("sub-frame words fail rather than appearing to preserve invisible words", () => {
  assert.throws(() => prepareCaptionTrack({
    text: "Hola", words: [{ word: "Hola", start: 1.001, end: 1.002 }],
  }, clips), /visible/u);
});
