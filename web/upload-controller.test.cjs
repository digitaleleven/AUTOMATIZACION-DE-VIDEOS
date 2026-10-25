const assert = require("node:assert/strict");
const test = require("node:test");
const { bindFileInput, createUploadController } = require("./upload-controller.js");

function fixture(overrides = {}) {
  const calls = [];
  const uploadedItem = { id: "upload-1", original_filename: "clip.MOV", status: "ready" };
  const view = {
    library: [],
    selectedId: null,
    notes: "briefing de prueba",
    messages: [],
    notifications: [],
    busy: [],
    rendered: 0,
  };
  const controller = createUploadController({
    upload: async (...args) => {
      calls.push(args);
      return { item: uploadedItem };
    },
    loadLibrary: async () => [uploadedItem],
    getLibrary: () => view.library,
    setLibrary: (items) => { view.library = items; },
    setSelectedId: (id) => { view.selectedId = id; },
    renderLibrary: () => { view.rendered += 1; },
    renderSelectedItem: () => { view.rendered += 1; },
    getNiche: () => "fashion",
    getStyleNotes: () => view.notes,
    clearStyleNotes: () => { view.notes = ""; },
    setBusy: (value) => view.busy.push(value),
    setMessage: (text, state) => view.messages.push({ text, state }),
    notify: (text, error = false) => view.notifications.push({ text, error }),
    ...overrides,
  });
  return { calls, controller, uploadedItem, view };
}

test("the file input change binding posts the selected video and preserves niche and briefing", async () => {
  const context = fixture();
  let changeHandler;
  const input = {
    files: [{ name: "clip.MOV", type: "video/quicktime" }],
    value: "C:\\fakepath\\clip.MOV",
    addEventListener(name, handler) {
      assert.equal(name, "change");
      changeHandler = handler;
    },
  };

  bindFileInput(input, context.controller);
  const result = await changeHandler({ currentTarget: input });

  assert.equal(result.status, "uploaded");
  assert.equal(context.calls.length, 1);
  assert.equal(context.calls[0][0], input.files[0]);
  assert.equal(context.calls[0][1], "fashion");
  assert.equal(context.calls[0][2], "briefing de prueba");
  assert.equal(context.view.selectedId, "upload-1");
  assert.equal(context.view.library[0].id, "upload-1");
  assert.equal(context.view.notes, "");
  assert.deepEqual(context.view.busy, [true, false]);
  assert.equal(input.value, "");
  assert.match(context.view.messages[0].text, /clip\.MOV/u);
});

test("a successful upload remains visible when the follow-up library refresh fails", async () => {
  const context = fixture({
    loadLibrary: async () => { throw new Error("refresh unavailable"); },
  });

  const result = await context.controller.handleSelection({ name: "clip.mp4" });

  assert.equal(result.status, "uploaded_refresh_failed");
  assert.equal(context.view.selectedId, "upload-1");
  assert.equal(context.view.library[0].id, "upload-1");
  assert.match(context.view.messages.at(-1).text, /Vídeo guardado/u);
  assert.doesNotMatch(context.view.messages.at(-1).text, /No se pudo cargar/u);
});

test("unsupported extensions are rejected without sending an upload request", async () => {
  const context = fixture();
  const result = await context.controller.handleSelection({ name: "clip.avi" });
  assert.equal(result.status, "invalid");
  assert.equal(context.calls.length, 0);
  assert.equal(context.view.messages[0].state.error, true);
});
