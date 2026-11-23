"use strict";

const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs/promises");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");
const { startDesktopSocket } = require("./desktop_socket.cjs");

const localId = "local_01234567-89ab-cdef-0123-456789abcdef";
const list = { tool: "getSessionList", input: {} };
const toggle = { tool: "toggleRemoteControl", input: { chat_id: localId, enabled: true } };

function exchange(endpoint, wire) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(endpoint, () => socket.end(wire));
    const chunks = [];
    socket.on("data", (chunk) => chunks.push(chunk));
    socket.on("error", reject);
    socket.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks)));
      } catch (error) {
        reject(error);
      }
    });
  });
}

async function fixture(t, dispatch) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "claudex-transport-"));
  let listener;
  t.after(async () => {
    await listener?.close();
    await fs.rm(directory, { recursive: true, force: true });
  });
  listener = await startDesktopSocket(dispatch, { runtimeDirectory: directory });
  const endpoint = path.join(directory, "claudex", "desktop.sock");
  return { directory, endpoint, listener };
}

const encode = (request) => JSON.stringify(request) + "\n";
const success = (result) => ({ status: "ok", result });

test("forwards allowlisted calls and preserves an explicit refusal", async (t) => {
  const refusal = { status: "refused", reason: "Approval denied." };
  const { endpoint } = await fixture(t, async (request) => request.tool === "getSessionList" ?
    success([{ chat_id: localId }]) : refusal);
  assert.equal((await fs.stat(endpoint)).mode & 0o7777, 0o600);
  assert.equal((await fs.stat(path.dirname(endpoint))).mode & 0o7777, 0o700);
  assert.deepEqual(await exchange(endpoint, encode(list)), success([{ chat_id: localId }]));
  assert.deepEqual(await exchange(endpoint, encode(toggle)), refusal);
});

test("rejects unknown tools, malformed frames and ambiguous targets before dispatch", async (t) => {
  let called = false;
  const { endpoint } = await fixture(t, async () => { called = true; });
  const invalidWires = [
    encode({ ...list, extra: true }),
    encode({ tool: "getSessionList", input: { extra: true } }),
    ...["createSession", "sendMessage", "respondToToolPermission", "runBashCommand", "delete"].map(
      (tool) => encode({ tool, input: { chat_id: localId } }),
    ),
    encode({ tool: "archive", input: { chat_id: localId, cleanupWorktree: true } }),
    encode({ tool: "setModel", input: { chat_id: localId, model: "a\u0000b" } }),
    encode({ tool: "setPermissionMode", input: { chat_id: localId, mode: "unrecognized" } }),
    encode({ ...toggle, input: { ...toggle.input, chat_id: ` ${localId}` } }),
    encode({ ...toggle, input: { ...toggle.input, chat_id: localId + "\n" } }),
    encode({ ...toggle, input: { ...toggle.input, chat_id: localId.toUpperCase() } }),
    encode({ ...toggle, input: { ...toggle.input, enabled: "true" } }),
    encode(list) + encode(toggle),
    JSON.stringify(list),
    Buffer.from([0xff, 0x0a]),
    " ".repeat(4096) + "\n",
  ];
  for (const wire of invalidWires) {
    assert.equal((await exchange(endpoint, wire)).status, "error");
  }
  assert.equal(called, false);
});

test("serializes dispatch and allows a later request after a handler settles", async (t) => {
  const { promise: dispatchEntered, resolve: entered } = Promise.withResolvers();
  const { promise: dispatchReleased, resolve: release } = Promise.withResolvers();
  const { endpoint } = await fixture(t, async (request) => {
    entered();
    await dispatchReleased;
    return success(request.tool);
  });
  const pending = exchange(endpoint, encode(toggle));
  await dispatchEntered;
  try {
    assert.deepEqual(await exchange(endpoint, encode(list)), {
      status: "error", reason: "The desktop handler is busy.",
    });
  } finally {
    release();
  }
  assert.deepEqual(await pending, success("toggleRemoteControl"));
  assert.deepEqual(await exchange(endpoint, encode(list)), success("getSessionList"));
});

test("does not displace another listener and cleans up on close", async (t) => {
  const { directory, endpoint, listener } = await fixture(t, async () => success([]));
  await assert.rejects(startDesktopSocket(async () => success([]), { runtimeDirectory: directory }), /already exists/);
  assert.deepEqual(await exchange(endpoint, encode(list)), success([]));
  await listener.close();
  await listener.close();
  assert.deepEqual(await fs.readdir(path.dirname(endpoint)), []);
});

test("refuses a public directory and a symlink before binding", async (t) => {
  const { directory } = await fixture(t, async () => success([]));
  const shared = path.join(directory, "shared");
  const symlink = path.join(directory, "symlink");
  await fs.mkdir(shared, { mode: 0o755 });
  await fs.symlink(shared, symlink);
  for (const runtimeDirectory of [shared, symlink]) {
    await assert.rejects(startDesktopSocket(async () => success([]), { runtimeDirectory }), /mode 0700/);
  }
});

test("reports handler failures and oversized replies without exposing internals", async (t) => {
  const { endpoint } = await fixture(t, async (request) => {
    if (request.tool === "toggleRemoteControl") throw new Error("Other account identifiers");
    return success(" ".repeat(512 * 1024));
  });
  assert.deepEqual(await exchange(endpoint, encode(toggle)), { status: "error", reason: "Desktop handler failed." });
  assert.deepEqual(await exchange(endpoint, encode(list)), {
    status: "error", reason: "Desktop response exceeds the size limit.",
  });
});

test("replaces a stale socket from an abruptly stopped Desktop", { timeout: 5000 }, async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "claudex-stale-"));
  const child = spawn(process.execPath, ["-e", `
    const { startDesktopSocket } = require(process.argv[1]);
    startDesktopSocket(async () => {}, { runtimeDirectory: process.argv[2] })
      .then(() => process.send("ready"));
  `, require.resolve("./desktop_socket.cjs"), directory], { stdio: ["ignore", "ignore", "inherit", "ipc"] });
  let listener;
  t.after(async () => {
    child.kill("SIGKILL");
    await listener?.close();
    await fs.rm(directory, { recursive: true, force: true });
  });
  const exited = once(child, "exit");
  await Promise.race([
    once(child, "message"),
    exited.then(() => assert.fail("fixture closed before binding the Desktop socket")),
  ]);
  child.kill("SIGKILL");
  await exited;
  const endpoint = path.join(directory, "claudex", "desktop.sock");
  assert.equal((await fs.stat(endpoint)).isSocket(), true);
  listener = await startDesktopSocket(async () => success([]), { runtimeDirectory: directory });
  assert.deepEqual(await exchange(endpoint, encode(list)), success([]));
  await listener.close();
  assert.deepEqual(await fs.readdir(path.dirname(endpoint)), []);
});
