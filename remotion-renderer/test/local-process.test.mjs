import assert from "node:assert/strict";
import test from "node:test";
import { runLocalProcess } from "../src/local-process.mjs";

test("local process returns output without shell interpolation", async () => {
  const result = await runLocalProcess(process.execPath, ["-e", "console.log('ok')"]);
  assert.equal(result.stdout.trim(), "ok");
});

test("nonzero exit preserves bounded stderr and exact error", async () => {
  await assert.rejects(
    runLocalProcess(process.execPath, ["-e", "console.error('x'.repeat(9000));process.exit(4)"], {
      maxStderrBytes: 1000,
    }),
    (error) => /code 4/u.test(error.message) && Buffer.byteLength(error.stderr) <= 1000,
  );
});

test("hung local process is terminated once without retry", async () => {
  const start = Date.now();
  await assert.rejects(
    runLocalProcess(process.execPath, ["-e", "setInterval(()=>{},1000)"], { timeoutMs: 150 }),
    /Tiempo máximo local/u,
  );
  assert.ok(Date.now() - start < 5000);
});

test("unbounded stdout fails instead of exhausting Node memory", async () => {
  await assert.rejects(
    runLocalProcess(process.execPath, ["-e", "console.log('x'.repeat(10000))"], { maxStdoutBytes: 1000 }),
    /límite de diagnóstico/u,
  );
});
