const assert = require("node:assert/strict");
const test = require("node:test");

const { createLibrarySelectionController } = require("./library-selection-controller.js");

test("selecting a library video opens Create with the same ID and does not upload it", () => {
  const events = [];
  const existingVideoId = "existing-video-123";
  const controller = createLibrarySelectionController({
    isAvailable: (id) => id === existingVideoId,
    setSelectedId: (id) => events.push(["select", id]),
    clearAuthorizations: () => events.push(["clear-authorizations"]),
    setActivePage: (page) => events.push(["navigate", page]),
    renderLibrary: () => events.push(["render-library"]),
    renderSelectedItem: () => events.push(["render-selection"]),
  });

  assert.equal(controller.selectExistingVideo(existingVideoId), true);
  assert.deepEqual(events, [
    ["select", existingVideoId],
    ["clear-authorizations"],
    ["navigate", "create"],
    ["render-library"],
    ["render-selection"],
  ]);
});

test("unknown or empty IDs do not change selection or navigate", () => {
  let effects = 0;
  const controller = createLibrarySelectionController({
    isAvailable: () => false,
    setSelectedId: () => effects++,
    setActivePage: () => effects++,
    renderLibrary: () => effects++,
    renderSelectedItem: () => effects++,
  });

  assert.equal(controller.selectExistingVideo("not-in-library"), false);
  assert.equal(controller.selectExistingVideo(""), false);
  assert.equal(effects, 0);
});
