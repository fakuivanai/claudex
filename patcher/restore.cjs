"use strict";

const launchAttempts = new WeakMap();

function hasExplicitRemoteControl(chat) {
  // Desktop hydrates the persisted On value into a pending user request.
  return chat.remoteControlUserEnabled === true || chat.remoteControlUserRequested === true;
}

function hasSavedRemoteControl(chat) {
  return (
    !chat.isArchived &&
    chat.backend.kind === "local" &&
    Boolean(chat.cliSessionId || chat.unarchivedCliSessionId) &&
    chat.remoteControlSpawn === undefined &&
    chat.movedToCloud === undefined &&
    !chat.scheduledTaskId &&
    !chat.stoppedUntilPersonSends &&
    !chat.remoteControlEndedByPolicy &&
    !chat.remoteControlUserToggled &&
    chat.remoteControlUserEnabled !== false &&
    (hasExplicitRemoteControl(chat) ||
      (chat.remoteControlAutoEligible === true && (chat.bridgeSessionIds?.length ?? 0) > 0))
  );
}

async function reconnect(chat, query, lifecycle, isCurrent, log, trigger) {
  if (
    !isCurrent() ||
    !hasSavedRemoteControl(chat) ||
    chat.query !== query ||
    chat.lastSpawnResumed !== true ||
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

function restoreAfterOpening(chat, query, lifecycle, isCurrent, log) {
  return reconnect(chat, query, lifecycle, isCurrent, log, "claudex_reopen");
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
  const canResume = (chat) =>
    isSignedIn() &&
    manager.sessions.get(chat.sessionId) === chat &&
    chat.homeTenancy?.accountId === account &&
    chat.homeTenancy?.orgId === organization &&
    hasSavedRemoteControl(chat) &&
    !chat.remoteControlEnabled &&
    !chat.remoteControlConnecting &&
    !chat.isRunning &&
    !chat.startResumeInFlight &&
    !chat.pendingRewindTo &&
    chat.interruptedByQuitAt === undefined &&
    !platform.hasPendingInput(chat) &&
    !manager.heldInputChecks.hasPendingUserInput(chat);

  if (account == null || !ownsRoster()) {
    return;
  }
  let pass = launchAttempts.get(manager);
  if (pass?.epoch !== epoch) {
    pass = { epoch, attempted: new WeakSet() };
    launchAttempts.set(manager, pass);
  }
  const candidates = [...manager.sessions.values()]
    .filter((chat) => hasSavedRemoteControl(chat) && !pass.attempted.has(chat))
    .sort((left, right) => {
      const preference = Number(hasExplicitRemoteControl(right)) -
        Number(hasExplicitRemoteControl(left));
      const recent = (chat) => chat.lastFocusedAt ?? chat.lastActivityAt ?? 0;
      return preference || recent(right) - recent(left);
    });
  if (candidates.length === 0) {
    return;
  }

  try {
    platform.log(`[claudex] Remote Control restart inspecting ${candidates.length} saved candidates`);
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
          chat, chat.query, manager.remoteControlLifecycle, () => canResume(chat),
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

module.exports = { hasSavedRemoteControl, restoreAfterOpening, restoreAtLaunch };
