"use strict";

const assert = require("node:assert/strict");
const { setImmediate: nextTick } = require("node:timers/promises");
const test = require("node:test");
const { registerPrepareThread } = require("../../claudex/codex/prepare.cjs");

const CHAT = "00000000-0000-4000-8000-000000000001";
const METHOD = "claudex-prepare-thread";
const saved = {
  id: CHAT,
  ephemeral: false,
  parentThreadId: null,
  source: "vscode",
  originator: "Codex Desktop",
  path: "/fixture/sessions/rollout.jsonl",
  cwd: "/fixture/project",
  canAcceptDirectInput: null,
};

function fixture(t, changes = {}) {
  const calls = [];
  let request;
  let matches;
  let complete;
  let owner = false;
  let current = true;
  let archive = false;
  const nativeResume = new Promise((resolve) => { complete = resolve; });
  const host = {
    getHostId: () => "local",
    isConversationArchiving: () => archive,
    isConversationSuppressedAfterArchive: () => false,
    getStreamRole: () => owner ? { role: "owner" } : null,
    getConversation: () => null,
    async readThread(id, options) {
      calls.push(["read", id, options]);
      return { thread: saved };
    },
    threadStore: {
      async getUnarchivedThreadIds(ids) {
        calls.push(["archive", ids]);
        return ids;
      },
    },
    retainActiveConversation(id) {
      calls.push(["retain", id]);
      return { [Symbol.dispose]: () => calls.push(["release", id]) };
    },
    async resumeConversationForUnavailableOwner(options) {
      calls.push(["resume", options]);
      const result = await nativeResume;
      owner = result.status === "ready";
      return result;
    },
    ...changes,
  };
  const dispose = registerPrepareThread({
    host,
    ipcClient: {
      addRequestHandler(method, predicate, handler) {
        assert.equal(method, METHOD);
        matches = predicate;
        request = handler;
        return () => calls.push(["unregister"]);
      },
    },
    assertCurrent() {
      if (!current) throw Error("Native host is retired.");
    },
  });
  t.after(dispose);

  function frame(action, changes = {}) {
    return { params: { hostId: "local", conversationId: CHAT, action, ...changes } };
  }

  return {
    calls, dispose, complete, frame,
    get matches() { return matches; },
    request: (action, changes) => request(frame(action, changes)),
    retire() { current = false; },
    archive() { archive = true; },
  };
}

test("registers only for the local host and only accepts an exact thread and action", async (t) => {
  const remote = fixture(t, { getHostId: () => "remote" });
  assert.equal(remote.matches, undefined);
  const local = fixture(t);
  assert.equal(local.matches(local.frame("check").params), true);
  assert.equal(local.matches(local.frame("check").params, { hostId: "remote" }), false);
  assert.deepEqual(await local.request("warm", { hostId: "remote" }), { state: "unavailable" });
  assert.deepEqual(await local.request("warm", { conversationId: "/fixture/project" }), { state: "unavailable" });
  assert.deepEqual(await local.request("warm", { turnStart: { input: [] } }), { state: "unavailable" });
  assert.deepEqual(await local.request("send"), { state: "unavailable" });
  assert.deepEqual(local.calls, []);
});

test("checks eligibility without retaining or resuming the chat", async (t) => {
  const local = fixture(t);
  assert.deepEqual(await local.request("check"), { state: "available" });
  assert.deepEqual(local.calls, [
    ["read", CHAT, { includeTurns: false }],
    ["archive", [CHAT]],
  ]);
});

test("rejects archived, ephemeral, read-only and subordinate chats", async (t) => {
  const archived = fixture(t, { threadStore: { getUnarchivedThreadIds: async () => [] } });
  assert.deepEqual(await archived.request("check"), { state: "unavailable" });
  for (const invalid of [
    { ephemeral: true },
    { canAcceptDirectInput: false },
    { parentThreadId: CHAT },
    { source: { subAgent: "fixture" } },
    { originator: "codex-cli" },
    { id: "00000000-0000-4000-8000-000000000002" },
    { path: null },
  ]) {
    const local = fixture(t, { readThread: async () => ({ thread: { ...saved, ...invalid } }) });
    assert.deepEqual(await local.request("check"), { state: "unavailable" });
  }
});

test("coalesces warm requests and confirms the native owner before reporting ready", async (t) => {
  const local = fixture(t);
  assert.deepEqual(await local.request("warm"), { state: "warming" });
  assert.deepEqual(await local.request("warm"), { state: "warming" });
  await nextTick();
  assert.deepEqual(local.calls, [
    ["read", CHAT, { includeTurns: false }],
    ["archive", [CHAT]],
    ["retain", CHAT],
    ["resume", {
      conversationId: CHAT,
      model: null,
      reasoningEffort: null,
      workspaceRoots: [saved.cwd],
      collaborationMode: null,
    }],
  ]);
  local.complete({ status: "ready" });
  await nextTick();
  assert.deepEqual(await local.request("check"), { state: "ready" });
  local.dispose();
  assert.equal(local.calls.filter(([operation]) => operation === "release").length, 1);
});

test("never reports ready after a failed native resume, archive or account change", async (t) => {
  for (const change of [null, "archive", "retire"]) {
    const local = fixture(t);
    assert.deepEqual(await local.request("warm"), { state: "warming" });
    await nextTick();
    if (change) local[change]();
    local.complete(change ? { status: "ready" } : { status: "not-ready" });
    await nextTick();
    assert.deepEqual(await local.request("warm"), { state: "unavailable" });
    assert.equal(local.calls.filter(([operation]) => operation === "release").length, 1);
  }
});

test("releases the native lease on deadline even if resume is still pending", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const local = fixture(t);
  assert.deepEqual(await local.request("warm"), { state: "warming" });
  await nextTick();
  assert.equal(local.calls.filter(([operation]) => operation === "release").length, 0);
  t.mock.timers.tick(60_000);
  assert.equal(local.calls.filter(([operation]) => operation === "release").length, 1);

  local.complete({ status: "ready" });
  await nextTick();
  local.dispose();
  local.dispose();
  assert.equal(local.calls.filter(([operation]) => operation === "release").length, 1);
  assert.equal(local.calls.filter(([operation]) => operation === "unregister").length, 1);
});

test("a status check does not report availability solely because a warm is pending", async (t) => {
  const pending = Promise.withResolvers();
  const invalid = { thread: { ...saved, ephemeral: true } };
  let calls = 0;
  const local = fixture(t, { readThread: async () => ++calls === 1 ? pending.promise : invalid });
  assert.deepEqual(await local.request("warm"), { state: "warming" });
  assert.deepEqual(await local.request("check"), { state: "unavailable" });
  pending.resolve(invalid);
  await nextTick();
  assert.deepEqual(local.calls, []);
});
