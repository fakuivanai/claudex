"use strict";

const launchAttempts = new WeakMap();

function matchesSavedChat(chat, pass) {
  return (
    // Native hydration retains the explicit setting and also requests reconnect
    // in memory. The latter can also come from an unsent first-turn draft.
    chat.remoteControlUserEnabled === true &&
    chat.homeTenancy?.accountId === pass.account &&
    chat.homeTenancy?.orgId === pass.organization &&
    // Stock yB requires a local chat with a saved backend.
    !chat.isArchived &&
    chat.backend.kind === "local" &&
    !!(chat.cliSessionId || chat.unarchivedCliSessionId) &&
    !chat.stoppedUntilPersonSends &&
    // Cloud moves, remotely started sessions and scheduled work have other owners.
    chat.movedToCloud === undefined &&
    chat.remoteControlSpawn === undefined &&
    !chat.scheduledTaskId &&
    !chat.remoteControlEndedByPolicy &&
    !chat.remoteControlUserToggled &&
    !chat.remoteControlDisconnecting
  );
}

async function reconnect(chat, query, lifecycle, pass, isCurrent, log, trigger) {
  if (
    !isCurrent() ||
    !matchesSavedChat(chat, pass) ||
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
    log(`[claudex] Remote Control ${trigger} ${chat.sessionId} ${status}`);
  } catch {
    log(`[claudex] Remote Control ${trigger} ${chat.sessionId} failed`);
  }
}

async function restoreAfterOpening(sessions, chat, query, lifecycle, isCurrent, log) {
  const pass = launchAttempts.get(sessions);
  const canReconnect = () => pass?.isSignedIn() && isCurrent();
  if (pass === undefined || !matchesSavedChat(chat, pass) || !canReconnect()) {
    return;
  }
  try {
    // Recheck current policy, including the native kill switch, after a deferred reopen.
    if (pass.platform.flagsReady() && await pass.platform.canRestore()) {
      await reconnect(chat, query, lifecycle, pass, canReconnect, log, "claudex_reopen");
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
      isSignedIn,
      attempted: new WeakSet(),
      platform,
    };
    launchAttempts.set(manager.sessions, pass);
  }
  const canResume = (chat) =>
    isSignedIn() &&
    manager.sessions.get(chat.sessionId) === chat &&
    matchesSavedChat(chat, pass) &&
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
    .filter((chat) => matchesSavedChat(chat, pass) && !pass.attempted.has(chat))
    .sort((left, right) => recent(right) - recent(left));
  if (candidates.length === 0) {
    return;
  }

  try {
    platform.log(`[claudex] Remote Control restart inspecting ${candidates.length} chats with Remote Control saved On`);
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

module.exports = { restoreAfterOpening, restoreAtLaunch };
