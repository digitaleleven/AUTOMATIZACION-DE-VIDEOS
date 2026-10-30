const test = require("node:test");
const assert = require("node:assert/strict");
const { createLibraryDeleteController } = require("./library-delete-controller.js");

function setup(overrides = {}) {
  const events = [];
  const controller = createLibraryDeleteController({
    confirmDelete: (item) => {
      events.push(["confirm", item.id]);
      return true;
    },
    deleteVideo: async (id) => events.push(["delete", id]),
    removeVideo: (id) => events.push(["remove", id]),
    notify: (message, error) => events.push(["notify", message, error]),
    ...overrides,
  });
  return { controller, events };
}

test("confirmed deletion calls the server for only the selected item then removes it from view", async () => {
  const { controller, events } = setup();

  assert.equal(await controller.deleteSelected({ id: "video-1", status: "ready" }), true);
  assert.deepEqual(events.slice(0, 3), [
    ["confirm", "video-1"],
    ["delete", "video-1"],
    ["remove", "video-1"],
  ]);
  assert.match(events[3][1], /resultados e historial.*se conservan/);
});

test("cancelling confirmation does not call the server or remove the item", async () => {
  const { controller, events } = setup({ confirmDelete: () => false });

  assert.equal(await controller.deleteSelected({ id: "video-1", status: "ready" }), false);
  assert.deepEqual(events, []);
});

test("processing videos are blocked before confirmation and server request", async () => {
  const { controller, events } = setup();

  assert.equal(await controller.deleteSelected({ id: "video-1", status: "processing" }), false);
  assert.equal(events.length, 1);
  assert.equal(events[0][0], "notify");
  assert.match(events[0][1], /procesamiento/);
});

test("server failure leaves the item visible and reports the error", async () => {
  const { controller, events } = setup({
    deleteVideo: async () => { throw new Error("disk error"); },
  });

  assert.equal(await controller.deleteSelected({ id: "video-1", status: "ready" }), false);
  assert.equal(events.some(([name]) => name === "remove"), false);
  assert.match(events.at(-1)[1], /disk error/);
  assert.equal(events.at(-1)[2], true);
});

test("duplicate clicks during a pending request cannot issue a second delete", async () => {
  let finishDelete;
  let deleteCalls = 0;
  const { controller, events } = setup({
    deleteVideo: () => {
      deleteCalls += 1;
      return new Promise((resolve) => { finishDelete = resolve; });
    },
  });
  const first = controller.deleteSelected({ id: "video-1", status: "ready" });
  const second = await controller.deleteSelected({ id: "video-1", status: "ready" });

  assert.equal(second, false);
  assert.equal(deleteCalls, 1);
  finishDelete();
  assert.equal(await first, true);
});
