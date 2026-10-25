import assert from "node:assert/strict";
import test from "node:test";
import { DEFAULT_CAPTION_STYLE, resolveCaptionStyle } from "../src/caption-style.mjs";

test("caption style defaults to pastel pink with high-contrast dark active text", () => {
  assert.deepEqual(resolveCaptionStyle({}), DEFAULT_CAPTION_STYLE);
  assert.equal(DEFAULT_CAPTION_STYLE.highlightColor, "#FFC0CB");
  assert.equal(DEFAULT_CAPTION_STYLE.textColor, "#202124");
});

test("configuration and explicit briefing lines override each caption token safely", () => {
  assert.deepEqual(
    resolveCaptionStyle(
      {
        font_family: "Roboto",
        highlight_color: "#00FF66",
        font_color: "#FFFFFF",
      },
      "Contexto editorial\nfontFamily=Montserrat\nhighlightColor=#FF2A85\ntextColor=#000000",
    ),
    {
      fontFamily: "Montserrat",
      highlightColor: "#FF2A85",
      textColor: "#000000",
    },
  );
});

test("invalid explicit colors and unsafe font strings fail instead of silently applying", () => {
  assert.throws(
    () => resolveCaptionStyle({}, "highlightColor=rosa"),
    /color hexadecimal/u,
  );
  assert.throws(
    () => resolveCaptionStyle({}, "fontFamily=Montserrat; color:red"),
    /nombre de fuente sencillo/u,
  );
});
