const assert = require("node:assert/strict");
const test = require("node:test");

const {
  createLibrarySelectionController,
  getLibraryAction,
  isErrorStatus,
} = require("./library-selection-controller.js");

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

test("create action opens the existing video in Create with its original ID", () => {
  const events = [];
  const controller = createLibrarySelectionController({
    isAvailable: (id) => id === "ready-video",
    setSelectedId: (id) => events.push(["select", id]),
    clearAuthorizations: () => events.push(["clear-authorizations"]),
    setActivePage: (page) => events.push(["navigate", page]),
    renderLibrary: () => events.push(["render-library"]),
    renderSelectedItem: () => events.push(["render-selection"]),
  });

  assert.equal(controller.createWithExistingVideo("ready-video"), true);
  assert.deepEqual(events, [
    ["select", "ready-video"],
    ["clear-authorizations"],
    ["navigate", "create"],
    ["render-library"],
    ["render-selection"],
  ]);
});

test("error review selects the video and focuses its error; non-error videos are rejected", () => {
  const events = [];
  const controller = createLibrarySelectionController({
    isAvailable: (id) => id === "failed-video",
    isErrorVideo: (id) => id === "failed-video",
    setSelectedId: (id) => events.push(["select", id]),
    clearAuthorizations: () => events.push(["clear-authorizations"]),
    setActivePage: (page) => events.push(["navigate", page]),
    renderLibrary: () => events.push(["render-library"]),
    renderSelectedItem: () => events.push(["render-selection"]),
    focusError: (id) => events.push(["focus-error", id]),
  });

  assert.equal(controller.reviewVideoError("failed-video"), true);
  assert.equal(controller.reviewVideoError("ready-video"), false);
  assert.deepEqual(events, [
    ["select", "failed-video"],
    ["clear-authorizations"],
    ["navigate", "create"],
    ["render-library"],
    ["render-selection"],
    ["focus-error", "failed-video"],
  ]);
});

test("library actions match the real status: create for ready, separate review for errors", () => {
  assert.equal(getLibraryAction("ready"), "create");
  assert.equal(getLibraryAction("failed"), "review_error");
  assert.equal(getLibraryAction("render_failed"), "review_error");
  assert.equal(getLibraryAction("processing"), null);
  assert.equal(isErrorStatus("interrupted"), true);
  assert.equal(isErrorStatus("completed"), false);
});
