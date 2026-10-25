const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const test = require("node:test");

const projectRoot = path.resolve(__dirname, "..");
const htmlSource = fs.readFileSync(path.join(projectRoot, "web/index.html"), "utf8");
const cssSource = fs.readFileSync(path.join(projectRoot, "web/premium.css"), "utf8");
const widths = [375, 402, 768, 1024, 1440];
const pages = ["home", "create", "library"];
const emptyStateSelectors = [
  ".recent-empty",
  ".empty-state",
  ".stage-placeholder",
  ".output-empty",
  ".job-empty",
  ".summary-thumbnail-empty",
  ".summary-empty",
];
const textSelectors = [
  ".recent-empty",
  ".empty-state h3",
  ".empty-state p",
  ".stage-placeholder p",
  ".output-empty strong",
  ".output-empty p",
  ".job-empty strong",
  ".job-empty p",
  ".summary-thumbnail-empty strong",
  ".summary-thumbnail-empty small",
  ".summary-empty",
];
const screenshotDirectory = path.join(projectRoot, "generated-artifacts/responsive-visual");
const resultMarker = "data-responsive-visual-results";
const expectedEmptyStates = {
  home: [".recent-empty"],
  create: [".stage-placeholder", ".output-empty", ".job-empty", ".summary-thumbnail-empty", ".summary-empty"],
  library: [".empty-state"],
};

const measurementScript = `
(() => {
  const stateSelectors = ${JSON.stringify(emptyStateSelectors)};
  const textSelectors = ${JSON.stringify(textSelectors)};
  const rectOf = (element) => {
    const rect = element.getBoundingClientRect();
    return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
  };
  const overlaps = (first, second) => {
    const a = rectOf(first);
    const b = rectOf(second);
    return Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1
      && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1;
  };
  const clippedByAncestor = (element, axis) => {
    const textRect = element.getBoundingClientRect();
    let ancestor = element.parentElement;
    while (ancestor) {
      const style = getComputedStyle(ancestor);
      const overflow = axis === "x" ? style.overflowX : style.overflowY;
      if (["hidden", "clip", "auto", "scroll"].includes(overflow)) {
        const rect = ancestor.getBoundingClientRect();
        const start = axis === "x" ? textRect.left - rect.left : textRect.top - rect.top;
        const end = axis === "x" ? rect.right - textRect.right : rect.bottom - textRect.bottom;
        if (start < -1 || end < -1) return true;
      }
      ancestor = ancestor.parentElement;
    }
    return false;
  };
  const emptyStates = stateSelectors.flatMap((selector) =>
    Array.from(document.querySelectorAll(selector))
      .filter((container) => {
        const rect = container.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      })
      .map((container) => {
        const children = Array.from(container.querySelectorAll("h3, p, strong, small, button"));
        if (container.matches(".recent-empty, .summary-empty")) children.push(container);
        const visibleChildren = children.filter((child) => {
          const rect = child.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0;
        });
        const collisions = [];
        for (let i = 0; i < visibleChildren.length; i += 1) {
          for (let j = i + 1; j < visibleChildren.length; j += 1) {
            if (overlaps(visibleChildren[i], visibleChildren[j])) {
              collisions.push({
                first: visibleChildren[i].className || visibleChildren[i].tagName.toLowerCase(),
                second: visibleChildren[j].className || visibleChildren[j].tagName.toLowerCase(),
              });
            }
          }
        }
        return { selector, collisions };
      }),
  );
  const clippedText = textSelectors.flatMap((selector) =>
    Array.from(document.querySelectorAll(selector), (element) => {
      const rect = element.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return [];
      const clippedX = element.scrollWidth > element.clientWidth + 1 || clippedByAncestor(element, "x");
      const clippedY = element.scrollHeight > element.clientHeight + 1 || clippedByAncestor(element, "y");
      return clippedX || clippedY
        ? [{ selector, text: element.textContent.trim(), clippedX, clippedY }]
        : [];
    }).flat(),
  );
  const result = {
    viewportWidth: window.innerWidth,
    documentWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
    overflowElements: Array.from(document.body.querySelectorAll("*"))
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return {
          tag: element.tagName.toLowerCase(),
          id: element.id,
          className: typeof element.className === "string" ? element.className : "",
          left: Math.round(rect.left),
          right: Math.round(rect.right),
          scrollWidth: element.scrollWidth,
          clientWidth: element.clientWidth,
        };
      })
      .filter((element) => element.right > window.innerWidth + 1)
      .slice(0, 20),
    emptyStates,
    clippedText,
  };
  const output = document.createElement("pre");
  output.id = "visual-results";
  output.setAttribute("${resultMarker}", "");
  output.hidden = true;
  output.textContent = JSON.stringify(result);
  document.body.append(output);
})();
`;

function createFixture(page) {
  const withoutScripts = htmlSource.replace(/\s*<script\b[^>]*>[\s\S]*?<\/script>/gi, "");
  const withoutStylesheet = withoutScripts.replace(
    /<link\s+rel="stylesheet"\s+href="\/premium\.css"\s*>/i,
    `<style>${cssSource}</style>`,
  );
  const withSelectedPage = withoutStylesheet.replace(
    "</head>",
    `<style>[data-page-section]:not([data-page-section="${page}"]) { display: none !important; }</style></head>`,
  );
  return withSelectedPage.replace(
    "<body>",
    `<body class="visual-page-${page}">`,
  ).replace("</body>", `<script>${measurementScript}</script></body>`);
}

function readResults(serializedResult) {
  assert.ok(serializedResult, "Chromium should return the browser measurements");
  return JSON.parse(serializedResult);
}

function readPngDimensions(filePath) {
  const png = fs.readFileSync(filePath);
  assert.equal(png.subarray(1, 4).toString(), "PNG", "Chromium should save a PNG screenshot");
  return { width: png.readUInt32BE(16), height: png.readUInt32BE(20) };
}

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function startBrowser(chromiumPath, profileDirectory) {
  const browser = spawn(
    chromiumPath,
    [
      "--headless",
      "--no-sandbox",
      "--disable-gpu",
      "--disable-background-networking",
      "--disable-sync",
      "--disable-default-apps",
      "--no-first-run",
      "--remote-debugging-address=127.0.0.1",
      "--remote-debugging-port=0",
      `--user-data-dir=${profileDirectory}`,
      "about:blank",
    ],
    { stdio: ["ignore", "ignore", "pipe"] },
  );
  let browserError = "";
  browser.stderr.setEncoding("utf8");
  browser.stderr.on("data", (chunk) => { browserError += chunk; });

  const activePortPath = path.join(profileDirectory, "DevToolsActivePort");
  const deadline = Date.now() + 10000;
  while (!fs.existsSync(activePortPath) && Date.now() < deadline) {
    if (browser.exitCode !== null) break;
    await delay(50);
  }
  assert.ok(
    fs.existsSync(activePortPath),
    `Chromium did not start its local debugging endpoint: ${browserError}`,
  );
  const [port] = fs.readFileSync(activePortPath, "utf8").trim().split("\n");
  const response = await fetch(`http://127.0.0.1:${port}/json/list`);
  assert.ok(response.ok, "Chromium's local debugging endpoint should respond");
  const targets = await response.json();
  const pageTarget = targets.find((target) => target.type === "page");
  assert.ok(pageTarget, "Chromium should open a page target");

  const socket = new WebSocket(pageTarget.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  let nextId = 0;
  const pending = new Map();
  const eventWaiters = new Map();
  socket.addEventListener("message", ({ data }) => {
    const message = JSON.parse(data);
    if (message.id) {
      const waiter = pending.get(message.id);
      if (waiter) {
        pending.delete(message.id);
        if (message.error) waiter.reject(new Error(message.error.message));
        else waiter.resolve(message.result);
      }
    } else if (message.method) {
      const waiters = eventWaiters.get(message.method) || [];
      eventWaiters.delete(message.method);
      for (const resolve of waiters) resolve(message.params);
    }
  });

  return {
    browser,
    async send(method, params = {}) {
      const id = ++nextId;
      const responsePromise = new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
      socket.send(JSON.stringify({ id, method, params }));
      return responsePromise;
    },
    waitForEvent(method, timeout = 15000) {
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`Timed out waiting for ${method}`)), timeout);
        const waiters = eventWaiters.get(method) || [];
        waiters.push((params) => {
          clearTimeout(timer);
          resolve(params);
        });
        eventWaiters.set(method, waiters);
      });
    },
    async close() {
      socket.close();
      if (browser.exitCode !== null) return;
      await new Promise((resolve) => {
        const timer = setTimeout(() => {
          browser.kill("SIGKILL");
          resolve();
        }, 2000);
        browser.once("exit", () => {
          clearTimeout(timer);
          resolve();
        });
        browser.kill("SIGTERM");
      });
    },
  };
}

async function capturePage(browser, fixturePath, screenshotPath, width) {
  await browser.send("Emulation.setDeviceMetricsOverride", {
    width,
    height: 1200,
    deviceScaleFactor: 1,
    mobile: width <= 700,
  });
  const loadEvent = browser.waitForEvent("Page.loadEventFired");
  await browser.send("Page.navigate", { url: `file://${fixturePath}` });
  await loadEvent;

  const evaluation = await browser.send("Runtime.evaluate", {
    expression: `document.querySelector("#visual-results")?.textContent || ""`,
    returnByValue: true,
  });
  assert.ok(evaluation.result.value, "the page should report browser measurements");
  const screenshot = await browser.send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: false,
    fromSurface: true,
  });
  fs.writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"));
  return readResults(evaluation.result.value);
}

test("la interfaz no desborda ni recorta texto en los anchos principales", async (t) => {
  const tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "matrix-motion-responsive-"));
  const profileDirectory = path.join(tempDirectory, "chromium-profile");
  fs.mkdirSync(screenshotDirectory, { recursive: true });
  for (const fileName of fs.readdirSync(screenshotDirectory)) {
    if (/^viewport-\d+\.png$/.test(fileName)) {
      fs.rmSync(path.join(screenshotDirectory, fileName));
    }
  }
  let browser;

  try {
    for (const page of pages) {
      fs.writeFileSync(path.join(tempDirectory, `${page}.html`), createFixture(page));
    }
    browser = await startBrowser(process.env.CHROMIUM_BIN || "chromium", profileDirectory);
    await browser.send("Page.enable");
    await browser.send("Runtime.enable");

    for (const width of widths) {
      for (const page of pages) {
        await t.test(`${width}px · ${page}`, async () => {
          const screenshotPath = path.join(screenshotDirectory, `viewport-${width}-${page}.png`);
          const fixturePath = path.join(tempDirectory, `${page}.html`);
          const result = await capturePage(browser, fixturePath, screenshotPath, width);

          assert.equal(result.viewportWidth, width, "the browser should use the requested viewport width");
          assert.ok(
            result.documentWidth <= width + 1,
            `horizontal page overflow: document is ${result.documentWidth}px wide; `
              + JSON.stringify(result.overflowElements),
          );
          assert.ok(
            expectedEmptyStates[page].every((selector) =>
              result.emptyStates.some((state) => state.selector === selector),
            ),
            `the expected empty states should be visible on ${page}`,
          );
          assert.deepEqual(
            result.emptyStates.flatMap((state) => state.collisions),
            [],
            "content elements should not overlap inside empty states",
          );
          assert.deepEqual(
            result.clippedText,
            [],
            "empty-state headings and descriptions should not be clipped",
          );
          assert.ok(fs.statSync(screenshotPath).size > 0, "a viewport screenshot should be saved");
          assert.deepEqual(
            readPngDimensions(screenshotPath),
            { width, height: 1200 },
            "the screenshot should use the requested viewport dimensions",
          );
          console.log(`Responsive screenshot: ${path.relative(projectRoot, screenshotPath)}`);
        });
      }
    }
  } finally {
    await browser?.close();
    fs.rmSync(tempDirectory, { recursive: true, force: true });
  }
});
