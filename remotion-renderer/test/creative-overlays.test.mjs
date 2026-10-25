import assert from "node:assert/strict";
import test from "node:test";
import { buildCreativeOverlays } from "../src/creative-overlays.mjs";

const defaults = {
  durationInFrames: 300,
  fps: 30,
  wordTimings: [
    { text: "Este", start: 1, end: 1.2 },
    { text: "sérum", start: 1.2, end: 1.5 },
    { text: "hidrata.", start: 1.5, end: 1.9 },
  ],
};
const build = (options = {}) => buildCreativeOverlays({ ...defaults, ...options });

test("empty optional overlay plan adds no invented title, image or brand", () => {
  assert.deepEqual(build(), { title: null, promo: null, images: [], cards: [], warnings: [] });
});

test("explicit briefing schedules title, promo and product by actual word timestamps", () => {
  const result = build({
    briefing: "Notas libres sin interpretar.\ntitle=Paso a paso\npromo=Publi / Marca\npip=serum.png|Sérum|serum|right\ncard=Hidratación|hidrata|left",
  });
  assert.equal(result.title.text, "Paso a paso");
  assert.equal(result.title.durationFrames, 105);
  assert.equal(result.promo.text, "Publi / Marca");
  assert.equal(result.images[0].startFrame, 36);
  assert.equal(result.cards[0].startFrame, 45);
  assert.equal(result.images[0].durationFrames, 75);
});

test("multiword reference matches accent and punctuation safely", () => {
  const result = build({ planOverlays: {
    images: [{ asset: "serum.webp", trigger_text: "ESTE SERUM", position: "topRight" }],
  } });
  assert.equal(result.images[0].startFrame, 30);
});

test("missing spoken reference is omitted with a warning, not fabricated timing", () => {
  const result = build({ briefing: "pip=serum.png|Sérum|no existe|right" });
  assert.equal(result.images.length, 0);
  assert.equal(result.warnings.length, 1);
});

test("explicit frame schedule is supported and clamped to output duration", () => {
  const result = build({ planOverlays: {
    cards: [{ text: "Referencia", start_seconds: 9, duration_seconds: 2.5 }],
  } });
  assert.equal(result.cards[0].startFrame, 270);
  assert.equal(result.cards[0].durationFrames, 30);
});

test("remote resources, path traversal and invalid placement are rejected", () => {
  for (const asset of ["../secret.png", "https://example.org/a.png", "script.svg"]) {
    assert.throws(() => build({ planOverlays: {
      images: [{ asset, trigger_text: "sérum" }],
    } }), /archivos PNG/u);
  }
  assert.throws(() => build({ briefing: "card=Referencia|sérum|fullscreen" }), /posición/u);
});

test("invalid durations, malformed plans, and colliding placements fail explicitly", () => {
  assert.throws(() => build({ planOverlays: { images: "bad" } }), /lista/u);
  assert.throws(() => build({ planOverlays: {
    cards: [{ text: "Referencia", start_seconds: 1, duration_seconds: 0.1 }],
  } }), /duración/u);
  assert.throws(() => build({ briefing: "card=Uno|sérum|left\ncard=Dos|hidrata|left" }), /misma posición/u);
});
