"use strict";

const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs/promises");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");
const { setImmediate: nextTurn } = require("node:timers/promises");
const { MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, startDesktopSocket } = require("../../claudex/patch/socket.cjs");

const localId = "local_01234567-89ab-cdef-0123-456789abcdef";
const list = { tool: "getSessionList", input: {} };
const toggle = { tool: "toggleRemoteControl", input: { chat_id: localId, enabled: true } };

const start = {
  tool: "startSession",
  input: { chat_id: localId, folder: "/tmp/example", model: "sonnet", effort: "high",
    sender_chat_id: "12345678-89ab-cdef-0123-456789abcdef" },
};

function exchange(endpoint, wire, sent) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(endpoint, () => socket.end(wire, sent));
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

test("forwards the native start fields", async (t) => {
  const { endpoint } = await fixture(t, async (request) => success(request));
  assert.deepEqual(await exchange(endpoint, encode(start)), success(start));
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
    ...[
      { folder: "relative/path" }, { folder: "/tmp/folder\n" }, { folder: "/tmp/\ud800" },
      { sender_chat_id: "forged sender" }, { effort: "unrecognized" }, { message: "peer text" },
      { permission_mode: "bypassPermissions" }, { permissionMode: "bypassPermissions" },
    ].map((fields) => encode({ ...start, input: { ...start.input, ...fields } })),
    encode({ tool: "archive", input: { chat_id: localId, cleanupWorktree: true } }),
    encode({ tool: "setModel", input: { chat_id: localId, model: "a\u0000b" } }),
    encode({ tool: "setPermissionMode", input: { chat_id: localId, mode: "" } }),
    encode({ tool: "setEffort", input: { chat_id: localId, effort: "unrecognized" } }),
    encode({ ...toggle, input: { ...toggle.input, chat_id: ` ${localId}` } }),
    encode({ ...toggle, input: { ...toggle.input, chat_id: localId + "\n" } }),
    encode({ ...toggle, input: { ...toggle.input, chat_id: localId.toUpperCase() } }),
    encode({ ...toggle, input: { ...toggle.input, enabled: "true" } }),
    encode(list) + encode(toggle),
    JSON.stringify(list),
    Buffer.from([0xff, 0x0a]),
    " ".repeat(MAX_REQUEST_BYTES) + "\n",
  ];
  for (const wire of invalidWires) {
    assert.equal((await exchange(endpoint, wire)).status, "error");
  }
  assert.equal(called, false);
});

test("queues requests in arrival order and forwards them one at a time", async (t) => {
  const { promise: dispatchEntered, resolve: entered } = Promise.withResolvers();
  const { promise: dispatchReleased, resolve: release } = Promise.withResolvers();
  const calls = [];
  let active = 0;
  const { endpoint } = await fixture(t, async (request) => {
    calls.push(request.tool);
    assert.equal(++active, 1);
    entered();
    await dispatchReleased;
    active--;
    return success(request.tool);
  });
  const first = exchange(endpoint, encode(toggle));
  await dispatchEntered;
  try {
    const { promise: secondSent, resolve: sent } = Promise.withResolvers();
    const second = exchange(endpoint, encode(list), sent);
    await secondSent;
    await nextTurn();
    const third = exchange(endpoint, encode({ tool: "getSession", input: { chat_id: localId } }));
    assert.deepEqual(calls, ["toggleRemoteControl"]);
    release();
    assert.deepEqual(await Promise.all([first, second, third]), [
      success("toggleRemoteControl"), success("getSessionList"), success("getSession"),
    ]);
    assert.deepEqual(calls, ["toggleRemoteControl", "getSessionList", "getSession"]);
  } finally {
    release();
  }
});

test("does not displace another listener and cleans up on close", async (t) => {
  const { directory, endpoint, listener } = await fixture(t, async () => success([]));
  await assert.rejects(startDesktopSocket(async () => success([]), { runtimeDirectory: directory }), /already exists/);
  assert.deepEqual(await exchange(endpoint, encode(list)), success([]));
  await listener.close();
  await listener.close();
  assert.deepEqual(await fs.readdir(path.dirname(endpoint)), []);
});

test("will not replace a directory symlink or an unrelated endpoint file", async (t) => {
  const runtimeDirectory = await fs.mkdtemp(path.join(os.tmpdir(), "claudex-path-"));
  t.after(() => fs.rm(runtimeDirectory, { recursive: true, force: true }));
  const directory = path.join(runtimeDirectory, "claudex");
  await fs.symlink(runtimeDirectory, directory);
  await assert.rejects(startDesktopSocket(async () => success([]), { runtimeDirectory }), /not a directory/);
  await fs.unlink(directory);
  await fs.mkdir(directory);
  const endpoint = path.join(directory, "desktop.sock");
  await fs.writeFile(endpoint, "existing file");
  await assert.rejects(startDesktopSocket(async () => success([]), { runtimeDirectory }), /non-socket/);
  assert.equal(await fs.readFile(endpoint, "utf8"), "existing file");
});

test("reports handler failures and oversized replies without exposing internals", async (t) => {
  const { endpoint } = await fixture(t, async (request) => {
    if (request.tool === "toggleRemoteControl") throw new Error("Other account identifiers");
    return success(" ".repeat(MAX_RESPONSE_BYTES));
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
  `, require.resolve("../../claudex/patch/socket.cjs"), directory], { stdio: ["ignore", "ignore", "inherit", "ipc"] });
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
