"use strict";

const refusal = (reason) => ({ status: "refused", reason });
const failure = (reason) => ({ status: "error", reason });
const success = (result) => ({ status: "ok", result });
const SCOPE_CHANGED = failure("The active Desktop account or session roster changed during the request.");
const ROSTER_UNAVAILABLE = failure("The active Desktop roster is unavailable.");
const SESSION_UNAVAILABLE = refusal("The chat is unavailable in the active account and organization.");

// Desktop's set_session_permission_mode documents this order. dontAsk is a CLI
// mode outside that Desktop order. An unknown or incomparable mode cannot move.
const MODE_RANK = { plan: 0, default: 1, acceptEdits: 2, auto: 3, bypassPermissions: 4 };
const LOCAL_CHAT_ID = /^local_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/u;

function captureScope(manager, account) {
  const scope = manager.getScratchScope();
  const generation = manager.getLoadGeneration();
  if (manager.appQuitting || typeof scope?.accountId !== "string" || !scope.accountId ||
      typeof scope.orgId !== "string" || !scope.orgId || account() !== scope.accountId ||
      !Number.isSafeInteger(generation) || generation < 0) {
    return null;
  }
  return { accountId: scope.accountId, orgId: scope.orgId, generation };
}

function sameScope(current, previous) {
  return current !== null && current.accountId === previous.accountId &&
    current.orgId === previous.orgId && current.generation === previous.generation;
}

function ownsLocalChat(chat, scope) {
  return chat?.homeTenancy?.accountId === scope.accountId &&
    chat.homeTenancy.orgId === scope.orgId &&
    chat.backend?.kind === "local" && chat.backend.remoteTarget == null &&
    !chat.prewarmHidden && !chat.movedToCloud;
}

function projectSession(row, chat) {
  let remoteControl = "off";
  if (row.bridgeSessionUrl) remoteControl = "on";
  else if (row.remoteControlConnecting) remoteControl = "connecting";
  else if (row.remoteControlAvailable === false) remoteControl = "unavailable";
  return {
    chat_id: row.sessionId,
    title: typeof row.title === "string" ? row.title : null,
    running: row.isRunning === true,
    archived: row.isArchived === true,
    warm: chat.query != null,
    remote_control: remoteControl,
    remote_control_saved: typeof chat.remoteControlUserEnabled === "boolean" ? chat.remoteControlUserEnabled : null,
    model: typeof row.model === "string" ? row.model : null,
    effort: typeof row.effort === "string" ? row.effort : null,
    fast_mode: row.fastModeState === "on" ? true : row.fastModeState === "off" ? false : null,
    permission_mode: typeof row.permissionMode === "string" ? row.permissionMode : null,
  };
}

function canSetMode(current, desired) {
  return current === desired ||
    (Object.hasOwn(MODE_RANK, current) && Object.hasOwn(MODE_RANK, desired) &&
      MODE_RANK[desired] < MODE_RANK[current]);
}

// Do not forward raw errors or results. They can contain a workspace path
// or the active bridge URL. Map known lifecycle outcomes to user-readable text.
function remoteControlNotice(result) {
  switch (result?.reason) {
    case "compliance":
      return "Desktop's compliance settings disallow Remote Control.";
    case "unavailable":
      return "Remote Control is unavailable for this chat or deployment.";
    case "account_changed":
      return "Desktop's signed-in account changed during the request.";
    case "no_active_session":
      return "Desktop could not start an active worker for this chat.";
    case "timeout":
      return "Desktop timed out waiting for the Remote Control worker.";
    default:
      return "Desktop could not change this chat's Remote Control connection.";
  }
}

function createDesktopBridge(api, manager, { account, windowActive = () => true }) {
  return async function dispatch(request) {
    if (!windowActive()) return ROSTER_UNAVAILABLE;
    const scope = captureScope(manager, account);
    if (scope === null) return ROSTER_UNAVAILABLE;
    const current = () => windowActive() && sameScope(captureScope(manager, account), scope);
    await manager.waitForSessionsLoaded();
    if (!current()) return SCOPE_CHANGED;
    if (manager.isInitializing() || !manager.areSessionsLoaded()) return ROSTER_UNAVAILABLE;

    if (request.tool === "getSessionList") {
      const roster = await api.getSessionList();
      if (!current() || roster?.loadGeneration !== scope.generation) return SCOPE_CHANGED;
      if (!Array.isArray(roster.sessions) || typeof roster.archivedPending !== "boolean") {
        return ROSTER_UNAVAILABLE;
      }
      const sessions = [];
      for (const row of roster.sessions) {
        if (typeof row?.sessionId !== "string" || !LOCAL_CHAT_ID.test(row.sessionId) || row.isCloudRun) continue;
        const chat = manager.sessions.get(row.sessionId);
        if (ownsLocalChat(chat, scope)) sessions.push(projectSession(row, chat));
      }
      return success({ sessions, archived_pending: roster.archivedPending });
    }

    // Native getSession loads a deferred archived row when needed. Validate
    // ownership after its await and capture the exact roster entry for mutation.
    const id = request.input.chat_id;
    const row = await api.getSession(id);
    if (!current()) return SCOPE_CHANGED;
    const chat = manager.sessions.get(id);
    if (row?.sessionId !== id || row.isCloudRun || !ownsLocalChat(chat, scope)) return SESSION_UNAVAILABLE;
    const ownsTarget = () => current() && manager.sessions.get(id) === chat && ownsLocalChat(chat, scope);

    if (request.tool === "getSession") return success(projectSession(row, chat));
    if (chat.isArchived && request.tool !== "unarchive" && request.tool !== "archive") {
      return refusal("Unarchive this chat before changing it.");
    }

    let notice;
    switch (request.tool) {
      case "toggleRemoteControl": {
        const enabled = request.input.enabled;
        let result = await api.toggleRemoteControl(id, enabled);
        if (!ownsTarget()) return SCOPE_CHANGED;
        if (enabled && result?.reason === "no_active_session" && chat.remoteControlUserEnabled === true) {
          if (chat.isArchived) return refusal("The chat was archived during the request.");
          // The first native call saved On. Ask Desktop to warm exactly this
          // chat, then retry only if the saved choice still agrees.
          await api.warmSession(id);
          if (!ownsTarget()) return SCOPE_CHANGED;
          if (chat.isArchived) return refusal("The chat was archived during the request.");
          if (chat.remoteControlUserEnabled !== true) {
            return refusal("The chat's Remote Control setting changed while Desktop warmed it.");
          }
          if (!chat.query) {
            notice = "Remote Control is saved On. Desktop did not start a worker and the chat is disconnected.";
          } else if (!chat.bridgeSessionUrl) {
            // Native warm may be attaching through the saved-on path already.
            // Native toggle serializes with that same Remote Control transition.
            result = await api.toggleRemoteControl(id, true);
            if (!ownsTarget()) return SCOPE_CHANGED;
            if (result?.ok !== true && chat.remoteControlUserEnabled === true) {
              notice = `Remote Control is saved On. ${remoteControlNotice(result)}`;
            }
          } else {
            result = { ok: true };
          }
        }
        if (!notice && result?.ok !== true) return refusal(remoteControlNotice(result));
        break;
      }
      case "warmSession":
        await api.warmSession(id);
        if (!chat.query) notice = "Desktop did not start a worker for this chat.";
        break;
      case "setModel":
        await api.setModel(id, request.input.model);
        break;
      case "setEffort":
        await api.setEffort(id, request.input.effort ?? undefined);
        break;
      case "setFastMode":
        await api.setFastMode(id, request.input.enabled);
        break;
      case "setPermissionMode": {
        const currentMode = manager.permissionModeUnderHold(id) ?? chat.permissionMode;
        const mode = request.input.mode;
        if (!canSetMode(currentMode, mode)) {
          return refusal("Changing to that permission mode requires Desktop's window.");
        }
        if (chat.permissionMode === mode) return success({ session: projectSession(row, chat) });
        if (!await api.setPermissionMode(id, mode)) {
          return refusal("Desktop declined the permission mode change.");
        }
        break;
      }
      case "archive":
        if (!chat.isArchived) await api.archive(id, { cleanupWorktree: false });
        break;
      case "unarchive":
        if (chat.isArchived) await api.unarchive(id);
        break;
      case "stop":
        await api.stop(id);
        break;
      case "interrupt":
        await api.interrupt(id);
        break;
      default:
        return refusal("Unsupported Desktop action.");
    }

    if (!ownsTarget()) return SCOPE_CHANGED;
    const updated = await api.getSession(id);
    if (!ownsTarget()) return SCOPE_CHANGED;
    const session = updated?.sessionId === id ? projectSession(updated, chat) : null;
    if (request.tool === "toggleRemoteControl" && request.input.enabled &&
        session?.remote_control !== "on" && !notice) {
      notice = "Desktop accepted the toggle. The chat does not have a confirmed Remote Control connection.";
    }
    if (request.tool === "setFastMode" && session?.fast_mode !== request.input.enabled) {
      notice = "Desktop has not applied the requested fast mode to the current worker.";
    }
    return success(notice ? { session, notice } : { session });
  };
}

const windows = new Map();
let listener = null;
let starting = false;
let quitting = false;
let quitHookInstalled = false;

function attachDesktopWindow(window, api, manager, account, log) {
  const { app } = require("electron");
  const contents = window.webContents;
  if (!contents || contents.isDestroyed() || quitting) return;
  const dispatch = createDesktopBridge(api, manager, {
    account,
    windowActive: () => !contents.isDestroyed() && !quitting,
  });
  // The stock factory normally runs once per main window. Choose the most
  // recently registered surviving window when Desktop creates a replacement.
  windows.set(contents, dispatch);
  contents.once("destroyed", () => windows.delete(contents));
  if (listener || starting) return;
  starting = true;
  if (!quitHookInstalled) {
    quitHookInstalled = true;
    app.once("before-quit", () => {
      quitting = true;
      if (listener) void listener.close().catch(() => log("[claudex] Could not close the Desktop socket"));
    });
  }
  void app.whenReady().then(async () => {
    if (quitting || !app.hasSingleInstanceLock()) return;
    const { startDesktopSocket } = require("./claudex-desktop-socket.cjs");
    listener = await startDesktopSocket((request) => {
      const active = Array.from(windows.values()).at(-1);
      return active ? active(request) : ROSTER_UNAVAILABLE;
    });
    if (quitting) await listener.close();
    else log("[claudex] Desktop control socket ready");
  }).catch((error) => {
    log(`[claudex] Could not start the Desktop socket: ${error.code ?? error.message}`);
  }).finally(() => { starting = false; });
}

module.exports = { createDesktopBridge, attachDesktopWindow };
