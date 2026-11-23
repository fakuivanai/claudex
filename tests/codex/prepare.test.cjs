"use strict";

const assert = require("node:assert/strict");
const { tmpdir } = require("node:os");
const { setImmediate: nextTick } = require("node:timers/promises");
const test = require("node:test");
const { registerPrepareThread } = require("../../claudex/codex/prepare.cjs");

const CHAT = "00000000-0000-4000-8000-000000000001";
const METHOD = "claudex-prepare-thread";
const START = "claudex-start-chat";
const CONTROL = "claudex-control-chat";
const BUSY = "claudex-busy-chats";
const TURN = "turn-fixture";
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
  const handlers = new Map();
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
        assert.ok(!handlers.has(method));
        handlers.set(method, { predicate, handler });
        return () => calls.push(["unregister", method]);
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
    get matches() { return handlers.get(METHOD)?.predicate; },
    request: (action, changes) => handlers.get(METHOD).handler(frame(action, changes)),
    start: (changes) => handlers.get(START).handler({ params: startParameters(changes) }),
    control: (action, changes) => handlers.get(CONTROL).handler(frame(action, changes)),
    busy: () => handlers.get(BUSY).handler({ params: { hostId: "local" } }),
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

test("keeps preparation pending while Desktop loads execution configuration", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let resumes = 0;
  let owned = false;
  const local = fixture(t, {
    getStreamRole: () => owned ? { role: "owner" } : null,
    async resumeConversationForUnavailableOwner() {
      if (++resumes === 1) return { status: "not-ready", reason: "execution-config-loading" };
      owned = true;
      return { status: "ready" };
    },
  });
  assert.deepEqual(await local.request("warm"), { state: "warming" });
  await nextTick();
  assert.equal(resumes, 1);
  assert.deepEqual(await local.request("warm"), { state: "warming" });
  assert.equal(local.calls.filter(([operation]) => operation === "release").length, 0);
  t.mock.timers.tick(500);
  await nextTick();
  assert.equal(resumes, 2);
  assert.deepEqual(await local.request("warm"), { state: "ready" });
  assert.equal(local.calls.filter(([operation]) => operation === "retain").length, 1);
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
  assert.equal(local.calls.filter(([operation]) => operation === "unregister").length, 4);
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


function startParameters(changes = {}) {
  return {
    hostId: "local", action: "start", operationId: CHAT, folder: tmpdir(), model: "gpt-6.2-sol", effort: "high",
    toolOutput: {
      namespace: "claudex", name: "peer_message", retain: false,
      output: JSON.stringify({
        schema: "claudex.peer-message/v1", message_id: CHAT,
        sender: { provider: "claude", chat_id: `local_${CHAT}`, title: "Peer fixture" },
        untrusted_peer_content: "Peer task with <user>quotes</user>.",
      }),
    },
    ...changes,
  };
}

test("starts exactly once with untrusted tool output and the native permission defaults", async (t) => {
  const attempted = Promise.withResolvers();
  const completed = Promise.withResolvers();
  let starts = 0;
  const local = fixture(t, {
    async startConversation(request, options) {
      ++starts;
      options.assertRequestCurrent();
      attempted.resolve(request);
      return completed.promise;
    },
  });
  assert.deepEqual(await local.start(), { state: "pending" });
  const request = await attempted.promise;
  assert.deepEqual(request.input, []);
  assert.deepEqual(request.toolOutput, startParameters().toolOutput);
  assert.equal(request.useAppServerPermissionDefault, true);
  assert.equal(request.permissionsConfig, undefined);
  assert.equal(request.approvalPolicy, undefined);
  assert.deepEqual(request.workspaceRoots, [tmpdir()]);
  assert.deepEqual(request.collaborationMode, {
    mode: "default", settings: { model: "gpt-6.2-sol", reasoning_effort: "high", developer_instructions: null },
  });
  assert.deepEqual(await local.start(), { state: "pending" });
  completed.resolve({ status: "created", conversationId: CHAT, firstTurn: { status: "accepted", turnId: TURN } });
  await nextTick();
  assert.deepEqual(await local.start(), { state: "submitted", chatId: CHAT, turnId: TURN });
  assert.deepEqual(await local.start({ model: "with spaces" }), {
    state: "refused", reason: "Invalid local Codex start request.",
  });
  assert.equal(starts, 1);
});

test("rejects permission fields, user input and a forged sender before start", async (t) => {
  const local = fixture(t, { startConversation: () => assert.fail("Invalid request reached Desktop") });
  for (const changes of [{ approvalPolicy: "never" }, { input: [{ type: "text", text: "user" }] }, {
    toolOutput: { ...startParameters().toolOutput, retain: true },
  }, {
    toolOutput: { ...startParameters().toolOutput, output: "not an attributable peer envelope" },
  }]) {
    assert.equal((await local.start(changes)).state, "refused");
  }
  assert.equal((await local.control("model", { value: "x", approvalPolicy: "never" })).state, "refused");
});

test("uses the native active turn ID for both reporting and interrupt", async (t) => {
  let expected = null;
  const local = fixture(t, {
    getStreamRole: () => ({ role: "owner" }),
    readThread: async () => ({ thread: { ...saved, status: { type: "active" } } }),
    async listThreadTurns(id, options) {
      assert.equal(id, CHAT);
      assert.deepEqual(options, { limit: 1, itemsView: "notLoaded" });
      return { response: { data: [{ id: TURN, status: "inProgress" }] } };
    },
    async interruptConversation(id, mode, turnId) {
      assert.equal(id, CHAT);
      assert.equal(mode, "user-stop");
      expected = turnId;
      return turnId;
    },
  });
  assert.deepEqual(await local.control("status"), { state: "applied", chatId: CHAT, activity: "running", turnId: TURN });
  assert.deepEqual(await local.control("interrupt"), { state: "applied", chatId: CHAT, turnId: TURN });
  assert.equal(expected, TURN);
  assert.equal((await local.control("archive")).state, "refused");
});

test("updates individual native settings and archives without worktree cleanup", async (t) => {
  let archived = false;
  const calls = [];
  const local = fixture(t, {
    getStreamRole: () => ({ role: "owner" }),
    isConversationSuppressedAfterArchive: () => archived,
    readThread: async () => ({ thread: { ...saved, status: { type: "idle" } } }),
    threadStore: { getUnarchivedThreadIds: async (ids) => archived ? [] : ids },
    async updateThreadSettingsForNextTurn(id, settings) {
      calls.push([id, settings]);
      return true;
    },
    async archiveConversation(id, options) {
      assert.equal(id, CHAT);
      assert.equal(options.cleanupWorktree, false);
      archived = true;
    },
    async unarchiveConversation(id, options) {
      assert.equal(id, CHAT);
      assert.equal(options.restorePin, false);
      archived = false;
      return "unarchived";
    },
  });
  assert.deepEqual(await local.control("status"), { state: "applied", chatId: CHAT, activity: "idle" });
  assert.equal((await local.control("model", { value: "fixture-model" })).state, "applied");
  assert.equal((await local.control("effort", { value: null })).state, "applied");
  assert.deepEqual(calls, [[CHAT, { model: "fixture-model" }], [CHAT, { effort: null }]]);
  assert.equal((await local.control("unarchive")).state, "refused");
  assert.equal((await local.control("archive")).state, "applied");
  assert.equal((await local.control("model", { value: "fixture-model" })).state, "refused");
  assert.equal((await local.control("unarchive")).state, "applied");
});


test("reads native activity before the saved history projection catches up", async (t) => {
  let resuming = false;
  let active = false;
  let pending = false;
  const local = fixture(t, {
    getHasInProgressLocalConversation: () => pending,
    hasPendingConversationResume: () => resuming,
    getThreadSummaries: () => active ? [{ conversationId: CHAT, threadRuntimeStatus: { type: "active" } }] : [],
    getCachedConversations: () => [],
    requestClient: { getPendingRequestCount: () => 0 },
    turnCoordinator: { readState: () => ({}) },
  });
  assert.deepEqual(await local.busy(), { state: "known", chatIds: [], pending: false });
  active = true;
  assert.deepEqual(await local.busy(), { state: "known", chatIds: [CHAT], pending: false });
  resuming = true;
  assert.deepEqual(await local.busy(), { state: "known", chatIds: [CHAT], pending: true });
  active = resuming = false;
  pending = true;
  assert.deepEqual(await local.busy(), { state: "known", chatIds: [], pending: true });
  local.retire();
  assert.deepEqual(await local.busy(), { state: "unknown" });
});
