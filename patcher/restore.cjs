"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");

// A hidden file without a .json suffix stays outside Desktop's local-session loader.
const SNAPSHOT_NAME = ".claudex-remote-control-v1";
// A quit records only live workers. This also bounds reads of a malformed file.
const MAX_SNAPSHOT_BYTES = 64 * 1024;
const UUID = "[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}";
const BACKEND_ID = new RegExp(`^${UUID}$`, "i");
const CHAT_ID = new RegExp(`^local_${UUID}$`, "i");
const launchAttempts = new WeakMap();

function snapshotPath(manager) {
  // getStorageDir joins the selected Electron profile, account, and organization.
  const directory = manager.getStorageDir();
  return directory == null ? undefined : path.join(directory, SNAPSHOT_NAME);
}

function exactBackend(chat) {
  // The native restore path also accepts the temporarily unarchived backend ID.
  return chat.cliSessionId || chat.unarchivedCliSessionId;
}

function belongsTo(chat, account, organization) {
  // Native hydration establishes homeTenancy. A parked account may share the app process.
  return chat.homeTenancy?.accountId === account && chat.homeTenancy?.orgId === organization;
}

function captureAtQuit(manager, platform) {
  const account = manager.currentAccountId;
  const organization = manager.currentOrgId;
  const file = snapshotPath(manager);
  if (file === undefined || account == null || organization == null || platform.account() !== account) {
    return;
  }
  const chats = [...manager.sessions.values()].filter((chat) =>
    belongsTo(chat, account, organization) &&
    !chat.isArchived &&
    // While a user-requested disconnect settles, the preceding On bit can still be true.
    !chat.remoteControlDisconnecting &&
    platform.connected(chat) &&
    CHAT_ID.test(chat.sessionId) && BACKEND_ID.test(exactBackend(chat) ?? "")
  ).map((chat) => ({ chatId: chat.sessionId, backendId: exactBackend(chat) }));
  const snapshot = { version: 1, accountId: account, orgId: organization, capturedAt: Date.now(), chats };
  const temporary = `${file}.${randomUUID()}`;
  try {
    fs.writeFileSync(temporary, JSON.stringify(snapshot), { mode: 0o600, flag: "wx" });
    fs.renameSync(temporary, file);
    platform.log(`[claudex] Remote Control captured ${chats.length} connected chats at quit`);
  } catch {
    // An older quit is not a substitute if this snapshot could not be published.
    try { fs.rmSync(file, { force: true }); } catch { /* Desktop shutdown continues. */ }
    platform.log("[claudex] Remote Control quit snapshot could not be saved");
  } finally {
    try { fs.rmSync(temporary, { force: true }); } catch { /* Desktop shutdown continues. */ }
  }
}

function parseSnapshot(raw, account, organization, since) {
  const snapshot = JSON.parse(raw);
  if (snapshot?.version !== 1 || snapshot.accountId !== account || snapshot.orgId !== organization ||
      !Number.isSafeInteger(snapshot.capturedAt) || snapshot.capturedAt <= since ||
      !Array.isArray(snapshot.chats)) {
    return new Map();
  }
  const chats = new Map();
  for (const chat of snapshot.chats) {
    if (typeof chat?.chatId !== "string" || !CHAT_ID.test(chat.chatId) ||
        typeof chat.backendId !== "string" || !BACKEND_ID.test(chat.backendId) || chats.has(chat.chatId)) {
      return new Map();
    }
    chats.set(chat.chatId, chat.backendId);
  }
  return chats;
}

function consumeSnapshot(manager, account, organization, log) {
  const file = snapshotPath(manager);
  if (file === undefined) {
    return new Map();
  }
  try {
    // The launcher records stock launches so a pre-disable quit is never replayed.
    const since = Number(process.env.CLAUDEX_REMOTE_CONTROL_SINCE ?? "0");
    const entry = fs.lstatSync(file);
    const raw = entry.isFile() && entry.size <= MAX_SNAPSHOT_BYTES ? fs.readFileSync(file, "utf8") : "";
    fs.unlinkSync(file);
    return Number.isSafeInteger(since) && since >= 0 && raw
      ? parseSnapshot(raw, account, organization, since)
      : new Map();
  } catch (error) {
    if (error.code !== "ENOENT") {
      log("[claudex] Remote Control quit snapshot could not be used");
    }
    return new Map();
  }
}

function shouldCheckDeferredRoster(manager) {
  const file = snapshotPath(manager);
  return (launchAttempts.get(manager.sessions)?.pending.size ?? 0) > 0 ||
    (file !== undefined && fs.existsSync(file));
}

function matchesPendingChat(chat, pass) {
  return (
    pass.pending.has(chat.sessionId) &&
    pass.pending.get(chat.sessionId) === exactBackend(chat) &&
    belongsTo(chat, pass.account, pass.organization) &&
    // Stock yB checks archived and backend kind. A stopped worker waits for a person.
    !chat.isArchived &&
    chat.backend.kind === "local" &&
    !chat.stoppedUntilPersonSends &&
    // Cloud moves, remotely started sessions and scheduled work have other owners.
    chat.movedToCloud === undefined &&
    chat.remoteControlSpawn === undefined &&
    !chat.scheduledTaskId &&
    // A native policy disconnect or a newer explicit Off beats the quit snapshot.
    !chat.remoteControlEndedByPolicy &&
    !chat.remoteControlUserToggled &&
    chat.remoteControlUserEnabled !== false
  );
}

async function reconnect(chat, query, lifecycle, pass, isCurrent, log, trigger) {
  if (
    !isCurrent() ||
    !matchesPendingChat(chat, pass) ||
    chat.query !== query ||
    // Stock Send uses a resumed query for auto-enable, not a draft or a new session.
    chat.lastSpawnResumed !== true ||
    // Let a connection already running, or a native attach in progress, own this worker.
    chat.remoteControlEnabled ||
    chat.remoteControlConnecting ||
    chat.remoteControlAutoInFlight
  ) {
    return;
  }
  try {
    const status = await lifecycle.maybeAutoEnableRemoteControl(
      chat,
      chat.worktreePath || chat.cwd,
      trigger,
    );
    if (status === "connected") {
      pass.pending.delete(chat.sessionId);
    }
    log(`[claudex] Remote Control ${trigger} ${chat.sessionId} ${status}`);
  } catch {
    log(`[claudex] Remote Control ${trigger} ${chat.sessionId} failed`);
  }
}

async function restoreAfterOpening(sessions, chat, query, lifecycle, isCurrent, log) {
  const pass = launchAttempts.get(sessions);
  if (pass === undefined || !matchesPendingChat(chat, pass) || !isCurrent()) {
    return;
  }
  try {
    // Recheck current policy, including the native kill switch, after a deferred reopen.
    if (pass.platform.flagsReady() && await pass.platform.canRestore()) {
      await reconnect(chat, query, lifecycle, pass, isCurrent, log, "claudex_reopen");
    }
  } catch {
    log("[claudex] Remote Control reopen policy check failed");
  }
}

async function restoreAtLaunch(manager, epoch, platform) {
  const account = manager.currentAccountId;
  const organization = manager.currentOrgId;
  const ownsRoster = () =>
    manager.sessionLoadEpoch === epoch &&
    !manager.appQuitting &&
    manager.currentAccountId === account &&
    manager.currentOrgId === organization;
  const isSignedIn = () => ownsRoster() && platform.account() === account;
  if (account == null || organization == null || !ownsRoster()) {
    return;
  }
  let pass = launchAttempts.get(manager.sessions);
  if (pass?.epoch !== epoch || pass.account !== account || pass.organization !== organization) {
    pass = {
      epoch,
      account,
      organization,
      pending: consumeSnapshot(manager, account, organization, platform.log),
      attempted: new WeakSet(),
      platform,
    };
    launchAttempts.set(manager.sessions, pass);
  }
  const canResume = (chat) =>
    isSignedIn() &&
    manager.sessions.get(chat.sessionId) === chat &&
    matchesPendingChat(chat, pass) &&
    !chat.remoteControlEnabled &&
    !chat.remoteControlConnecting &&
    // Don't compete with user work, native resume or a pending history rewind.
    !chat.isRunning &&
    !chat.startResumeInFlight &&
    !chat.pendingRewindTo &&
    // Desktop's interrupted-work pass handles its own markers and held input.
    chat.interruptedByQuitAt === undefined &&
    !platform.isUnattended(chat) &&
    !manager.heldInputChecks.hasPendingUserInput(chat);
  const recent = (chat) => chat.lastFocusedAt ?? chat.lastActivityAt ?? 0;
  const candidates = [...manager.sessions.values()]
    .filter((chat) => matchesPendingChat(chat, pass) && !pass.attempted.has(chat))
    .sort((left, right) => recent(right) - recent(left));
  if (candidates.length === 0) {
    return;
  }

  try {
    platform.log(`[claudex] Remote Control restart inspecting ${candidates.length} quit snapshot candidates`);
    await manager.awaitFreshFlagsForBackgroundPass();
    if (!isSignedIn()) {
      return;
    }
    if (!platform.flagsReady()) {
      platform.log("[claudex] Remote Control restart waiting for native flags");
      return;
    }
    await platform.waitForOrganizationPolicy();
    if (!isSignedIn()) {
      return;
    }
    if (!(await platform.canRestore())) {
      platform.log("[claudex] Remote Control restart declined by native background checks");
      return;
    }
    let timedOutWorktree = false;
    let processLimit = false;
    for (const chat of candidates) {
      if (!isSignedIn()) {
        return;
      }
      if (!canResume(chat)) {
        continue;
      }
      if (chat.query == null) {
        if (timedOutWorktree) {
          continue;
        }
        if (manager.cliGovernor.headroom() <= 1) {
          processLimit = true;
          continue;
        }
        const blocked = await platform.checkWorktree(chat);
        if (blocked === "probe_timed_out") {
          timedOutWorktree = true;
          continue;
        }
        if (blocked || !canResume(chat)) {
          continue;
        }
        const wait = manager.warmLifecycle.takeWarmUpSlot();
        if (wait > 0) {
          await platform.wait(wait);
        }
        if (!canResume(chat)) {
          continue;
        }
        if (manager.cliGovernor.headroom() <= 1) {
          processLimit = true;
          continue;
        }
        pass.attempted.add(chat);
        try {
          await manager.warmSession(chat.sessionId, { warmTrigger: "launch_quit_resume" });
        } catch {
          platform.log(`[claudex] Remote Control worker warm failed ${chat.sessionId}`);
          continue;
        }
      }
      if (chat.query != null && canResume(chat)) {
        pass.attempted.add(chat);
        await reconnect(
          chat, chat.query, manager.remoteControlLifecycle, pass, () => canResume(chat),
          platform.log, "claudex_restart",
        );
      }
    }
    if (timedOutWorktree || processLimit) {
      const reason = timedOutWorktree ? "worktree probe timed out" : "process limit";
      platform.log(`[claudex] Remote Control restart left remaining cold chats for reopen: ${reason}`);
    }
  } catch {
    platform.log("[claudex] Remote Control restart pass failed");
  }
}

module.exports = { captureAtQuit, restoreAfterOpening, restoreAtLaunch, shouldCheckDeferredRoster };
