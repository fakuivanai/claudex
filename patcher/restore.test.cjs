"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { captureAtQuit, restoreAfterOpening, restoreAtLaunch, shouldCheckDeferredRoster } = require("./restore.cjs");

function savedChat(number, overrides = {}) {
  const suffix = number.toString(16).padStart(12, "0");
  return {
    sessionId: `local_10000000-0000-4000-8000-${suffix}`,
    cliSessionId: `20000000-0000-4000-8000-${suffix}`,
    backend: { kind: "local" },
    homeTenancy: { accountId: "account", orgId: "organization" },
    cwd: "/workspace",
    remoteControlUserEnabled: true,
    query: null,
    ...overrides,
  };
}

function fixture(testContext, chats) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "claudex-quit-test-"));
  testContext.after(() => fs.rmSync(directory, { force: true, recursive: true }));
  const file = path.join(directory, ".claudex-remote-control-v1");
  const events = [];
  const platform = {
    account: () => "account",
    connected: (chat) => chat.remoteControlEnabled,
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
    getStorageDir: () => directory,
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
  const seed = (...previouslyConnected) => captureAtQuit({
    ...manager,
    sessions: new Map(previouslyConnected.map((chat) => [chat.sessionId, chat])),
  }, { ...platform, connected: () => true });
  return { manager, platform, events, file, seed };
}

test("quit captures only connected chats belonging to the selected tenant", (context) => {
  const connected = savedChat(1, { remoteControlEnabled: true });
  const togglingOff = savedChat(2, { remoteControlEnabled: true, remoteControlDisconnecting: true });
  const historical = savedChat(3, { remoteControlEnabled: false, bridgeSessionIds: ["past-attachment"] });
  const otherTenant = savedChat(4, {
    remoteControlEnabled: true, homeTenancy: { accountId: "account", orgId: "different-organization" },
  });
  const { manager, platform, file } = fixture(context, [connected, togglingOff, historical, otherTenant]);
  captureAtQuit(manager, platform);
  assert.deepEqual(JSON.parse(fs.readFileSync(file, "utf8")).chats, [
    { chatId: connected.sessionId, backendId: connected.cliSessionId },
  ]);
});

test("quit leaves the previous account snapshot untouched after sign-out", (context) => {
  const { manager, platform, file, seed } = fixture(context, []);
  seed(savedChat(1));
  manager.currentAccountId = null;
  captureAtQuit(manager, platform);
  assert.equal(JSON.parse(fs.readFileSync(file, "utf8")).chats.length, 1);
});

test("a quit with nothing connected replaces the prior snapshot with an empty set", (context) => {
  const { manager, platform, file, seed } = fixture(context, []);
  seed(savedChat(1));
  captureAtQuit(manager, platform);
  assert.deepEqual(JSON.parse(fs.readFileSync(file, "utf8")).chats, []);
});

test("startup without a prior quit snapshot leaves historical On chats stopped", async (context) => {
  const historicallyEnabled = savedChat(1, { remoteControlAutoEligible: true, bridgeSessionIds: ["past"] });
  const { manager, platform, events } = fixture(context, [historicallyEnabled]);
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("startup invokes the native handler only for the exact prior-quit chat", async (context) => {
  const previouslyConnected = savedChat(1, { remoteControlUserRequested: true });
  const historicallyEnabled = savedChat(2, { remoteControlAutoEligible: true, bridgeSessionIds: ["past"] });
  const { manager, platform, events, seed } = fixture(context, [previouslyConnected, historicallyEnabled]);
  seed(previouslyConnected);
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, [
    ["warm", previouslyConnected.sessionId, { warmTrigger: "launch_quit_resume" }],
    ["reconnect", previouslyConnected.sessionId, previouslyConnected.cliSessionId, "/workspace", "claudex_restart"],
  ]);
});

test("startup consumes the sidecar once", async (context) => {
  const { manager, platform, file, seed } = fixture(context, [savedChat(1)]);
  seed(savedChat(1));
  await restoreAtLaunch(manager, 7, platform);
  assert.equal(fs.existsSync(file), false);
});

test("a changed backend under the same chat ID does not restore", async (context) => {
  const previous = savedChat(1);
  const newBackend = { ...previous, cliSessionId: savedChat(2).cliSessionId };
  const { manager, platform, events, seed } = fixture(context, [newBackend]);
  seed(previous);
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("a malformed snapshot is never partially applied", async (context) => {
  const valid = savedChat(1);
  const { manager, platform, events, file, seed } = fixture(context, [valid]);
  seed(valid);
  const snapshot = JSON.parse(fs.readFileSync(file, "utf8"));
  snapshot.chats.push({ chatId: "other", backendId: "invalid" });
  fs.writeFileSync(file, JSON.stringify(snapshot));
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("an intervening stock launch invalidates a prior quit snapshot", async (context) => {
  const chat = savedChat(1);
  const { manager, platform, events, seed } = fixture(context, [chat]);
  seed(chat);
  const previousSince = process.env.CLAUDEX_REMOTE_CONTROL_SINCE;
  process.env.CLAUDEX_REMOTE_CONTROL_SINCE = String(Date.now());
  context.after(() => {
    if (previousSince === undefined) {
      delete process.env.CLAUDEX_REMOTE_CONTROL_SINCE;
    } else {
      process.env.CLAUDEX_REMOTE_CONTROL_SINCE = previousSince;
    }
  });
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("current saved Off overrides the snapshot", async (context) => {
  const chat = savedChat(1, { remoteControlUserEnabled: false });
  const { manager, platform, events, seed } = fixture(context, [chat]);
  seed(savedChat(1));
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("native managed policy can decline all background starts", async (context) => {
  const chat = savedChat(1);
  const { manager, platform, events, seed } = fixture(context, [chat]);
  seed(chat);
  platform.canRestore = async () => false;
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("an account epoch change while checking the worktree cancels the launch pass", async (context) => {
  const chat = savedChat(1);
  const { manager, platform, events, seed } = fixture(context, [chat]);
  seed(chat);
  platform.checkWorktree = async () => {
    manager.sessionLoadEpoch += 1;
    return false;
  };
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("a changed organization prevents warming a record from the previous organization", async (context) => {
  const chat = savedChat(1);
  const { manager, platform, events, seed } = fixture(context, [chat]);
  seed(chat);
  platform.canRestore = async () => {
    manager.currentOrgId = "other";
    return true;
  };
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("worker limit still permits reconnecting an already running worker", async (context) => {
  const cold = savedChat(1);
  const warm = savedChat(2, { query: {}, lastSpawnResumed: true });
  const { manager, platform, events, seed } = fixture(context, [cold, warm]);
  seed(cold, warm);
  manager.cliGovernor.headroom = () => 1;
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, [["reconnect", warm.sessionId, warm.cliSessionId, "/workspace", "claudex_restart"]]);
});

test("startup leaves a chat with saved pending input stopped", async (context) => {
  const chat = savedChat(1);
  const { manager, platform, events, seed } = fixture(context, [chat]);
  seed(chat);
  manager.heldInputChecks.hasPendingUserInput = () => true;
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events, []);
});

test("a deferred roster can evaluate a pending snapshot chat", async (context) => {
  const active = savedChat(1);
  const deferred = savedChat(2);
  const { manager, platform, events, seed } = fixture(context, [active]);
  seed(active, deferred);
  await restoreAtLaunch(manager, 7, platform);
  manager.sessions.set(deferred.sessionId, deferred);
  assert.equal(shouldCheckDeferredRoster(manager), true);
  await restoreAtLaunch(manager, 7, platform);
  assert.deepEqual(events.filter(([type]) => type === "warm").map(([, id]) => id), [
    active.sessionId, deferred.sessionId,
  ]);
});

test("reopen uses the active worktree for a snapshot chat left cold at startup", async (context) => {
  const chat = savedChat(1, { worktreePath: "/worktree" });
  const { manager, platform, events, seed } = fixture(context, [chat]);
  seed(chat);
  manager.cliGovernor.headroom = () => 1;
  await restoreAtLaunch(manager, 7, platform);
  chat.query = {};
  chat.lastSpawnResumed = true;
  await restoreAfterOpening(manager.sessions, chat, chat.query, manager.remoteControlLifecycle, () => true, platform.log);
  assert.deepEqual(events, [["reconnect", chat.sessionId, chat.cliSessionId, "/worktree", "claudex_reopen"]]);
});

test("reopen does not attach an unrelated historical chat", async (context) => {
  const relevant = savedChat(1);
  const loaded = savedChat(2, { query: {}, lastSpawnResumed: true });
  const { manager, platform, events, seed } = fixture(context, [relevant, loaded]);
  seed(relevant);
  manager.cliGovernor.headroom = () => 1;
  await restoreAtLaunch(manager, 7, platform);
  await restoreAfterOpening(manager.sessions, loaded, loaded.query, manager.remoteControlLifecycle, () => true, platform.log);
  assert.deepEqual(events, []);
});

test("reopen does not attach a stale query", async (context) => {
  const chat = savedChat(1);
  const { manager, platform, events, seed } = fixture(context, [chat]);
  seed(chat);
  manager.cliGovernor.headroom = () => 1;
  await restoreAtLaunch(manager, 7, platform);
  chat.query = {};
  chat.lastSpawnResumed = true;
  await restoreAfterOpening(manager.sessions, chat, {}, manager.remoteControlLifecycle, () => true, platform.log);
  assert.deepEqual(events, []);
});

test("reopen rechecks managed policy that changed after startup", async (context) => {
  const chat = savedChat(1);
  const { manager, platform, events, seed } = fixture(context, [chat]);
  seed(chat);
  manager.cliGovernor.headroom = () => 1;
  await restoreAtLaunch(manager, 7, platform);
  chat.query = {};
  chat.lastSpawnResumed = true;
  platform.canRestore = async () => false;
  await restoreAfterOpening(manager.sessions, chat, chat.query, manager.remoteControlLifecycle, () => true, platform.log);
  assert.deepEqual(events, []);
});
