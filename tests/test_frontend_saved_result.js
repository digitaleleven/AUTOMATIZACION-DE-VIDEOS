const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
const start = source.indexOf("async function restoreSelectedJob(item) {");
const end = source.indexOf("\nfunction updateCreationSummary", start);
assert.ok(start >= 0 && end > start);

function loadHelper(overrides) {
  const context = {
    displayToast: () => {},
    whiteLabelText: (text) => text,
    ...overrides,
  };
  vm.runInNewContext(
    `${source.slice(start, end)}\nglobalThis.restore = restoreSelectedJob;`,
    context,
  );
  return context.restore;
}

test("loads the persisted job and its real video/SEO data on source selection", async () => {
  const item = { id: "source-a", latest_job_id: "job-a", original_filename: "IMG_1254.MOV" };
  const job = { id: "job-a", output_id: "result.mp4", gemini_analysis: { copy_and_seo: { hook: "saved hook" } } };
  const calls = [];
  const restore = loadHelper({
    loadJob: async (id) => { assert.equal(id, "job-a"); return job; },
    getSelectedItem: () => item,
    renderJob: (...args) => calls.push(args),
  });
  await restore(item);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], job);
  assert.equal(calls[0][1], "IMG_1254.MOV");
});

test("does not show a stale video's result if selection changed during loading", async () => {
  const item = { id: "source-a", latest_job_id: "job-a" };
  let resolve;
  let rendered = false;
  const restore = loadHelper({
    loadJob: () => new Promise((done) => { resolve = done; }),
    getSelectedItem: () => ({ id: "source-b", latest_job_id: "job-b" }),
    renderJob: () => { rendered = true; },
  });
  const pending = restore(item);
  resolve({ id: "job-a" });
  await pending;
  assert.equal(rendered, false);
});

test("does not request a job for a source with no saved execution", async () => {
  const restore = loadHelper({ loadJob: () => assert.fail("unexpected request") });
  await restore({ id: "new-source" });
});
