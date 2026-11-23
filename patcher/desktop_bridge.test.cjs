"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { createDesktopBridge } = require("./desktop_bridge.cjs");

const id = "local_01234567-89ab-cdef-0123-456789abcdef";
const other = "local_11111111-2222-3333-4444-555555555555";
const scope = { accountId: "account", orgId: "org" };

function fixture() {
  const calls = [];
  const native = {
    signedIn: scope.accountId,
    generation: 2,
    heldMode: undefined,
    row: {
      sessionId: id,
      homeTenancy: scope,
      backend: { kind: "local" },
      query: null,
      permissionMode: "default",
      remoteControlAvailable: true,
      remoteControlUserEnabled: false,
      title: "Fixture",
    },
  };
  const manager = {
    getScratchScope: () => scope,
    getLoadGeneration: () => native.generation,
    waitForSessionsLoaded: async () => {},
    areSessionsLoaded: () => true,
    isInitializing: () => false,
    permissionModeUnderHold: () => native.heldMode,
    sessions: new Map([[id, native.row]]),
  };
  const api = {
    getSessionList: async () => ({
      sessions: [...manager.sessions.values()], loadGeneration: native.generation, archivedPending: true,
    }),
    getSession: async (chatId) => manager.sessions.get(chatId),
    toggleRemoteControl: async (chatId, enabled) => {
      calls.push(["toggle", chatId, enabled]);
      native.row.remoteControlUserEnabled = enabled;
      if (enabled && !native.row.query) return { ok: false, reason: "no_active_session" };
      native.row.bridgeSessionUrl = enabled ? "private URL" : undefined;
      return { ok: true };
    },
    warmSession: async (chatId) => {
      calls.push(["warm", chatId]);
      native.row.query = {};
    },
    setPermissionMode: async (chatId, mode) => {
      calls.push(["permission", chatId, mode]);
      native.row.permissionMode = mode;
      return true;
    },
    archive: async (chatId, options) => {
      calls.push(["archive", chatId, options]);
      native.row.isArchived = true;
    },
    unarchive: async (chatId) => {
      calls.push(["unarchive", chatId]);
      native.row.isArchived = false;
    },
  };
  const dispatch = createDesktopBridge(api, manager, { account: () => native.signedIn });
  const run = (tool, input = {}) => dispatch({ tool, input: { chat_id: id, ...input } });
  return { api, manager, native, calls, dispatch, run };
}

test("projects only this scope's local sessions and keeps URLs private", async () => {
  const { native, manager, dispatch } = fixture();
  manager.sessions.set(other, { ...native.row, sessionId: other, homeTenancy: { ...scope, orgId: "other" } });
  native.row.bridgeSessionUrl = "private URL";
  const response = await dispatch({ tool: "getSessionList", input: {} });
  assert.equal(response.status, "ok");
  assert.equal(response.result.archived_pending, true);
  assert.equal(response.result.sessions.length, 1);
  assert.equal(response.result.sessions[0].chat_id, id);
  assert.equal(response.result.sessions[0].remote_control, "on");
  assert.equal(response.result.sessions[0].remote_control_saved, false);
  assert.doesNotMatch(JSON.stringify(response), /private URL/);
});

test("forwards warm and cold toggles to the exact native methods", async () => {
  const { run, calls } = fixture();
  const on = await run("toggleRemoteControl", { enabled: true });
  assert.deepEqual(calls, [["toggle", id, true], ["warm", id], ["toggle", id, true]]);
  assert.equal(on.result.session.remote_control, "on");
  assert.equal(on.result.session.remote_control_saved, true);
  assert.equal(on.result.session.warm, true);
  const off = await run("toggleRemoteControl", { enabled: false });
  assert.deepEqual(calls.at(-1), ["toggle", id, false]);
  assert.equal(off.result.session.remote_control, "off");
  assert.equal(off.result.session.remote_control_saved, false);
});

test("recognizes an auto-attached worker and reports a missing worker separately", async () => {
  const { api, native, run, calls } = fixture();
  api.warmSession = async () => {};
  const pending = await run("toggleRemoteControl", { enabled: true });
  assert.equal(pending.result.session.remote_control_saved, true);
  assert.equal(pending.result.session.remote_control, "off");
  assert.match(pending.result.notice, /did not start a worker/);
  assert.deepEqual(calls, [["toggle", id, true]]);

  api.warmSession = async () => {
    native.row.query = {};
    native.row.bridgeSessionUrl = "private URL";
  };
  const on = await run("toggleRemoteControl", { enabled: true });
  assert.equal(on.result.session.remote_control, "on");
  assert.deepEqual(calls, [["toggle", id, true], ["toggle", id, true]]);
});

test("does not retry a cold toggle across an account switch or an explicit Off", async () => {
  for (const change of ["account", "off", "replacement", "archive"]) {
    const { api, native, run, calls, manager } = fixture();
    api.warmSession = async () => {
      if (change === "account") native.signedIn = "another account";
      if (change === "off") native.row.remoteControlUserEnabled = false;
      if (change === "replacement") manager.sessions.set(id, { ...native.row });
      if (change === "archive") native.row.isArchived = true;
    };
    const response = await run("toggleRemoteControl", { enabled: true });
    assert.notEqual(response.status, "ok");
    assert.deepEqual(calls, [["toggle", id, true]]);
  }
});

test("retains native compliance refusals without warming or exposing internals", async () => {
  const { api, run, calls } = fixture();
  api.toggleRemoteControl = async () => ({ ok: false, reason: "compliance", error: "native private details" });
  const response = await run("toggleRemoteControl", { enabled: true });
  assert.equal(response.status, "refused");
  assert.match(response.reason, /compliance/);
  assert.doesNotMatch(response.reason, /private/);
  assert.deepEqual(calls, []);
});

test("keeps permission changes at or below Desktop's effective mode", async () => {
  const { native, calls, run } = fixture();
  for (const mode of ["acceptEdits", "auto", "bypassPermissions", "dontAsk"]) {
    assert.equal((await run("setPermissionMode", { mode })).status, "refused");
  }
  native.row.permissionMode = "auto";
  native.heldMode = "default";
  assert.equal((await run("setPermissionMode", { mode: "acceptEdits" })).status, "refused");
  assert.deepEqual(calls, []);
  assert.equal((await run("setPermissionMode", { mode: "plan" })).status, "ok");
  assert.deepEqual(calls, [["permission", id, "plan"]]);
});

test("calls native archive and unarchive without requesting worktree cleanup", async () => {
  const { run, calls } = fixture();
  const archived = await run("archive");
  assert.equal(archived.result.session.archived, true);
  assert.equal((await run("toggleRemoteControl", { enabled: true })).status, "refused");
  const unarchived = await run("unarchive");
  assert.equal(unarchived.result.session.archived, false);
  assert.deepEqual(calls, [["archive", id, { cleanupWorktree: false }], ["unarchive", id]]);
});
