"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { restoreAfterOpening, restoreAtLaunch } = require("./restore.cjs");

function savedChat(number, overrides = {}) {
  const suffix = number.toString(16).padStart(12, "0");
  return {
    sessionId: `local_10000000-0000-4000-8000-${suffix}`,
    cliSessionId: `20000000-0000-4000-8000-${suffix}`,
    backend: { kind: "local" },
    homeTenancy: { accountId: "account", orgId: "organization" },
    cwd: "/workspace",
    remoteControlUserEnabled: true,
    remoteControlUserRequested: true,
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
    isUnattended: () => false,
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
        chat.remoteControlEnabled = true;
        return "connected";
      },
    },
  };
  return { manager, platform, events };
}

test("startup restores saved On chats without an earlier quit", async () => {
  const savedOn = savedChat(1, { remoteControlAutoEligible: false });
  const automatic = savedChat(2, {
    remoteControlUserEnabled: undefined,
    remoteControlUserRequested: undefined,
    remoteControlAutoEligible: true,
    bridgeSessionIds: ["old-link"],
  });
  const unsentDraft = savedChat(3, {
    remoteControlUserEnabled: undefined, remoteControlStartChoice: true,
  });
  const { manager, platform, events } = fixture([savedOn, automatic, unsentDraft]);
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, [
    ["warm", savedOn.sessionId, { warmTrigger: "launch_quit_resume" }],
    ["reconnect", savedOn.sessionId, savedOn.cliSessionId, "/workspace", "claudex_restart"],
  ]);
});

test("startup skips ineligible chats even when Remote Control was saved On", async () => {
  const chats = [
    { homeTenancy: { accountId: "other", orgId: "organization" } },
    { homeTenancy: { accountId: "account", orgId: "other" } },
    { isArchived: true },
    { backend: { kind: "ssh" } },
    { cliSessionId: undefined },
    { stoppedUntilPersonSends: true },
    { movedToCloud: {} },
    { remoteControlSpawn: {} },
    { scheduledTaskId: "task" },
    { remoteControlEndedByPolicy: true },
    { remoteControlUserToggled: true },
    { remoteControlDisconnecting: true },
  ].map((overrides, number) => savedChat(number, overrides));
  const { manager, platform, events } = fixture(chats);
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("setting Remote Control to Off during the worktree check stops restore", async () => {
  const chat = savedChat(1);
  const { manager, platform, events } = fixture([chat]);
  platform.checkWorktree = async () => {
    chat.remoteControlUserEnabled = false;
    return false;
  };
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("native managed policy can decline all background starts", async () => {
  const chat = savedChat(1);
  const { manager, platform, events } = fixture([chat]);
  platform.canRestore = async () => false;
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("an account epoch change while checking the worktree cancels the launch pass", async () => {
  const chat = savedChat(1);
  const { manager, platform, events } = fixture([chat]);
  platform.checkWorktree = async () => {
    manager.sessionLoadEpoch += 1;
    return false;
  };
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("a changed organization prevents warming a record from the previous organization", async () => {
  const chat = savedChat(1);
  const { manager, platform, events } = fixture([chat]);
  platform.canRestore = async () => {
    manager.currentOrgId = "other";
    return true;
  };
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("worker limit still permits reconnecting an already warm worker", async () => {
  const cold = savedChat(1);
  const warm = savedChat(2, { query: {}, lastSpawnResumed: true });
  const { manager, platform, events } = fixture([cold, warm]);
  manager.cliGovernor.headroom = () => 1;
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, [["reconnect", warm.sessionId, warm.cliSessionId, "/workspace", "claudex_restart"]]);
});

test("startup leaves a chat with saved pending input stopped", async () => {
  const chat = savedChat(1);
  const { manager, platform, events } = fixture([chat]);
  manager.heldInputChecks.hasPendingUserInput = () => true;
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("a deferred roster restores newly loaded saved On chats", async () => {
  const active = savedChat(1);
  const deferred = savedChat(2);
  const { manager, platform, events } = fixture([active]);
  await restoreAtLaunch(manager, 7, platform);
  manager.sessions.set(deferred.sessionId, deferred);
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events.filter(([type]) => type === "warm").map(([, id]) => id), [
    active.sessionId, deferred.sessionId,
  ]);
});

test("reopen uses the active worktree for a chat left cold at startup", async () => {
  const chat = savedChat(1, { worktreePath: "/worktree" });
  const { manager, platform, events } = fixture([chat]);
  manager.cliGovernor.headroom = () => 1;
  await restoreAtLaunch(manager, 7, platform);
  chat.query = {};
  chat.lastSpawnResumed = true;
  await restoreAfterOpening(manager.sessions, chat, chat.query, manager.remoteControlLifecycle, () => true, platform.log);
  assert.deepEqual(events, [["reconnect", chat.sessionId, chat.cliSessionId, "/worktree", "claudex_reopen"]]);
});

test("reopen skips an old chat without an explicit On setting", async () => {
  const relevant = savedChat(1);
  const loaded = savedChat(2, {
    query: {},
    lastSpawnResumed: true,
    remoteControlUserEnabled: undefined,
    remoteControlUserRequested: undefined,
    remoteControlAutoEligible: true,
  });
  const { manager, platform, events } = fixture([relevant, loaded]);
  manager.cliGovernor.headroom = () => 1;
  await restoreAtLaunch(manager, 7, platform);
  await restoreAfterOpening(manager.sessions, loaded, loaded.query, manager.remoteControlLifecycle, () => true, platform.log);
  assert.deepEqual(events, []);
});

test("reopen does not attach a stale query", async () => {
  const chat = savedChat(1);
  const { manager, platform, events } = fixture([chat]);
  manager.cliGovernor.headroom = () => 1;
  await restoreAtLaunch(manager, 7, platform);
  chat.query = {};
  chat.lastSpawnResumed = true;
  await restoreAfterOpening(manager.sessions, chat, {}, manager.remoteControlLifecycle, () => true, platform.log);
  assert.deepEqual(events, []);
});

test("reopen rechecks managed policy that changed after startup", async () => {
  const chat = savedChat(1);
  const { manager, platform, events } = fixture([chat]);
  manager.cliGovernor.headroom = () => 1;
  await restoreAtLaunch(manager, 7, platform);
  chat.query = {};
  chat.lastSpawnResumed = true;
  platform.canRestore = async () => false;
  await restoreAfterOpening(manager.sessions, chat, chat.query, manager.remoteControlLifecycle, () => true, platform.log);
  assert.deepEqual(events, []);
});
