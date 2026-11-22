"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { hasSavedRemoteControl, restoreAfterOpening, restoreAtLaunch } = require("./restore.cjs");

function savedChat(overrides = {}) {
  return {
    sessionId: "saved-chat",
    cliSessionId: "existing-backend",
    backend: { kind: "local" },
    homeTenancy: { accountId: "account", orgId: "organization" },
    cwd: "/workspace",
    remoteControlUserEnabled: true,
    remoteControlAutoEligible: false,
    query: null,
    ...overrides,
  };
}

function fixture(chats) {
  const events = [];
  const platform = {
    account: () => "account",
    flagsReady: () => true,
    waitForOrganizationPolicy: async () => {},
    canRestore: async () => true,
    hasPendingInput: () => false,
    checkWorktree: async () => false,
    wait: async () => {},
    log: () => {},
  };
  const manager = {
    sessionLoadEpoch: 7,
    currentAccountId: "account",
    currentOrgId: "organization",
    sessions: new Map(chats.map((chat) => [chat.sessionId, chat])),
    awaitFreshFlagsForBackgroundPass: async () => {},
    heldInputChecks: { hasPendingUserInput: () => false },
    cliGovernor: { headroom: () => 5 },
    warmLifecycle: { takeWarmUpSlot: () => 0 },
    warmSession: async (sessionId, options) => {
      events.push(["warm", sessionId, options]);
      const chat = manager.sessions.get(sessionId);
      chat.query = {};
      chat.lastSpawnResumed = true;
    },
    remoteControlLifecycle: {
      maybeAutoEnableRemoteControl: async (chat, directory, trigger) => {
        events.push(["reconnect", chat.sessionId, chat.cliSessionId, directory, trigger]);
        return "connected";
      },
    },
  };
  return { manager, platform, events };
}

test("recognizes explicit saved preference when automatic eligibility is false", () => {
  assert.equal(hasSavedRemoteControl(savedChat()), true);
});

test("recognizes a saved On choice hydrated as a pending native request", () => {
  assert.equal(hasSavedRemoteControl(savedChat({
    remoteControlUserEnabled: undefined, remoteControlUserRequested: true,
  })), true);
});

test("an explicit off wins over earlier attachment history", () => {
  assert.equal(hasSavedRemoteControl(savedChat({
    remoteControlUserEnabled: false, remoteControlUserRequested: true,
    remoteControlAutoEligible: true, bridgeSessionIds: ["prior"],
  })), false);
});

test("a default-on chat with prior attachment is eligible", () => {
  assert.equal(hasSavedRemoteControl(savedChat({
    remoteControlUserEnabled: undefined, remoteControlAutoEligible: true, bridgeSessionIds: ["prior"],
  })), true);
});

test("eligibility alone does not treat a never attached chat as default on", () => {
  assert.equal(hasSavedRemoteControl(savedChat({
    remoteControlUserEnabled: undefined, remoteControlAutoEligible: true,
  })), false);
});

test("startup uses the native worker resume and policy handler for the saved backend", async () => {
  const { manager, platform, events } = fixture([savedChat()]);
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, [
    ["warm", "saved-chat", { warmTrigger: "launch_quit_resume" }],
    ["reconnect", "saved-chat", "existing-backend", "/workspace", "claudex_restart"],
  ]);
});

test("native managed policy can decline all background starts", async () => {
  const { manager, platform, events } = fixture([savedChat()]);
  platform.canRestore = async () => false;
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("an account epoch change while checking the worktree cancels the launch pass", async () => {
  const { manager, platform, events } = fixture([savedChat()]);
  platform.checkWorktree = async () => {
    manager.sessionLoadEpoch += 1;
    return false;
  };
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("a changed organization prevents warming a record from the previous organization", async () => {
  const { manager, platform, events } = fixture([savedChat()]);
  platform.canRestore = async () => {
    manager.currentOrgId = "other";
    return true;
  };
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("worker limit leaves a cold chat for a subsequent reopen", async () => {
  const { manager, platform, events } = fixture([savedChat()]);
  manager.cliGovernor.headroom = () => 1;
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("worker limit still permits reconnecting an already running worker", async () => {
  const cold = savedChat();
  const warm = savedChat({ sessionId: "warm", query: {}, lastSpawnResumed: true });
  const { manager, platform, events } = fixture([cold, warm]);
  manager.cliGovernor.headroom = () => 1;
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, [["reconnect", "warm", "existing-backend", "/workspace", "claudex_restart"]]);
});

test("startup gives explicit saved preferences the first available workers", async () => {
  const defaultOn = savedChat({
    sessionId: "default-on", remoteControlUserEnabled: undefined,
    remoteControlAutoEligible: true, bridgeSessionIds: ["prior"], lastFocusedAt: 49,
  });
  const explicitOn = savedChat({
    remoteControlUserEnabled: undefined, remoteControlUserRequested: true, lastFocusedAt: 42,
  });
  const { manager, platform, events } = fixture([defaultOn, explicitOn]);
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events.filter(([kind]) => kind === "warm").map(([, id]) => id), [
    "saved-chat", "default-on",
  ]);
});

test("startup leaves a chat with saved pending input stopped", async () => {
  const { manager, platform, events } = fixture([savedChat()]);
  manager.heldInputChecks.hasPendingUserInput = () => true;
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("a deferred roster adds the new chat without retrying previous attempts", async () => {
  const { manager, platform, events } = fixture([savedChat()]);
  await restoreAtLaunch(manager, 7, platform);
  const deferred = savedChat({ sessionId: "deferred-chat" });
  manager.sessions.set(deferred.sessionId, deferred);
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events.filter(([type]) => type === "warm").map(([, id]) => id), [
    "saved-chat", "deferred-chat",
  ]);
});

test("reopen uses the active worktree for the same native policy handler", async () => {
  const query = {};
  const chat = savedChat({ query, lastSpawnResumed: true, worktreePath: "/worktree" });
  const { manager, events, platform } = fixture([chat]);
  await restoreAfterOpening(chat, query, manager.remoteControlLifecycle, () => true, platform.log);
  assert.deepEqual(events, [["reconnect", "saved-chat", "existing-backend", "/worktree", "claudex_reopen"]]);
});

test("reopen does not attach a stale query", async () => {
  const chat = savedChat({ query: {}, lastSpawnResumed: true });
  const { manager, events, platform } = fixture([chat]);
  await restoreAfterOpening(chat, {}, manager.remoteControlLifecycle, () => true, platform.log);
  assert.deepEqual(events, []);
});
