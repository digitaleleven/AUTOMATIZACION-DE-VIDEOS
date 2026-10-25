const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appSource = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
const indexSource = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
const helperStart = appSource.indexOf("const HIDDEN_TEST_UPLOAD_IDS = new Set(");
const helperEnd = appSource.indexOf("\n\nconst state =", helperStart);
assert.notEqual(helperStart, -1, "test-only visibility IDs should exist in app.js");
assert.notEqual(helperEnd, -1, "visibility helpers should remain separate from application state");

const context = {};
vm.runInNewContext(
  `${appSource.slice(helperStart, helperEnd)}\n`
    + "globalThis.filterItems = getVisibleLibraryItems;",
  context,
);

test("the six known test uploads are hidden without hiding future uploads by filename", () => {
  const items = [
    { id: "074e33f3a6004af388135a2e339480c1", original_filename: "IMG_1254.MOV" },
    { id: "21f0c12763ef425e921e286cd0d12dd8", original_filename: "IMG_1254.MOV" },
    { id: "2a4f73637d2a4edf86d0e2c91b6ea0c9", original_filename: "IMG_1254.MOV" },
    { id: "d96ccf90a12e45e79def4ddb6c8e217a", original_filename: "IMG_1254.MOV" },
    { id: "e263a3e2c522487f9e0cad5f99b7250b", original_filename: "IMG_1254.MOV" },
    { id: "f8c270f8b2a74f4a88f13990f82af564", original_filename: "IMG_1254.MOV" },
    { id: "new-upload-id", original_filename: "IMG_1254.MOV" },
    { id: "ordinary-upload-id", original_filename: "other-video.mp4" },
  ];

  const visibleIds = Array.from(context.filterItems(items), (item) => item.id);
  assert.deepEqual(visibleIds, ["new-upload-id", "ordinary-upload-id"]);
});

test("invalid library values produce an empty visible list", () => {
  assert.deepEqual(Array.from(context.filterItems(null)), []);
});

test("legacy result sections stay hidden without removing their media sources", () => {
  const hiddenResults = [
    ["resultado-fase1", "/media/outputs/IMG_1254-MATRIX-MOTION-FASE1.mp4"],
    ["resultado-mm003", "/media/outputs/IMG_1254-MATRIX-MOTION-MM003.mp4"],
    ["resultado-mm004", "/media/outputs/IMG_1254-MATRIX-MOTION-MM004.mp4"],
  ];

  for (const [id, mediaPath] of hiddenResults) {
    const section = indexSource.match(new RegExp(`<section\\b[^>]*id="${id}"[^>]*>`));
    assert.ok(section, `result section ${id} should remain in markup`);
    assert.match(section[0], /\shidden(?:\s|>)/, `${id} result section should be hidden`);
    assert.ok(indexSource.includes(mediaPath), `${id} media should be preserved`);
  }
});

test("the visible panel is white-labeled and exposes the 4K render and SEO summary controls", () => {
  for (const page of ["home", "create", "library", "settings"]) {
    assert.match(indexSource, new RegExp(`data-page="${page}"`));
    assert.match(indexSource, new RegExp(`data-page-section="${page}"`));
  }
  assert.match(indexSource, /MATRIX MOTION \| VIDEO AUTOMATION STUDIO • PRO EDITION/);
  assert.match(indexSource, /Motor MATRIX Engine: Configuración pendiente/);
  assert.match(indexSource, /Estrategia Copywriting &amp; SEO/);
  assert.match(indexSource, /id="copy-seo-button"/);
  assert.match(indexSource, /value="2160p"/);
  assert.match(indexSource, /value="60"/);
  assert.match(indexSource, /Mis últimos vídeos/);
  assert.match(indexSource, /Conectar Google Drive \(OAuth\)/);
  const visibleText = indexSource.replace(/<[^>]*>/g, " ");
  assert.doesNotMatch(visibleText, /Modal|Replit|Cloudflare|FFmpeg|Entorno local/i);
  assert.doesNotMatch(indexSource, /retry-render-button|retry-final-render-button/);
  assert.match(appSource, /copy-seo-button"\)\?\.addEventListener\("click"/);
  assert.match(appSource, /async function copySeoStrategy/);
});
