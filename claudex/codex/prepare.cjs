"use strict";

const { stat } = require("node:fs/promises");
const { isAbsolute } = require("node:path");

const METHOD = "claudex-prepare-thread";
const START = "claudex-start-chat";
const CONTROL = "claudex-control-chat";
const BUSY = "claudex-busy-chats";
const HOST = "local";
const MAX_STARTS = 64;
const EFFORTS = new Set(["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const DESKTOP_SOURCES = new Set(["vscode", "exec", "appServer", "app_server"]);
// Native idle-thread eviction can otherwise race the client's next request.
const RETENTION_MS = 60_000;
// Desktop briefly refuses native resume before startup settings are ready.
const EXECUTION_CONFIG_RETRY_MS = 400;

function hasFields(value, ...names) {
  return !!value && typeof value === "object" && !Array.isArray(value) &&
    Object.keys(value).length === names.length &&
    names.every(name => Object.hasOwn(value, name));
}

function localRequest(params, frame) {
  return params?.hostId === HOST && (frame?.hostId == null || frame.hostId === HOST);
}

function identifier(value) {
  return typeof value === "string" && UUID.test(value);
}

function validModel(model) {
  return typeof model === "string" && /^[^\s\u0000-\u001f\u007f]{1,128}$/u.test(model);
}

function validEffort(effort) {
  return effort === null || EFFORTS.has(effort);
}

function peerOutput(output, id) {
  if (!hasFields(output, "name", "namespace", "output", "retain") || output.name !== "peer_message" ||
      output.namespace !== "claudex" || output.retain !== false ||
      typeof output.output !== "string" || output.output.length > 128_000) return false;
  try {
    const value = JSON.parse(output.output);
    return hasFields(value, "schema", "message_id", "sender", "untrusted_peer_content") &&
      value.schema === "claudex.peer-message/v1" && value.message_id === id &&
      hasFields(value.sender, "provider", "chat_id", "title") &&
      value.sender.provider === "claude" && typeof value.sender.chat_id === "string" &&
      value.sender.chat_id.startsWith("local_") && identifier(value.sender.chat_id.slice(6)) &&
      typeof value.sender.title === "string" && typeof value.untrusted_peer_content === "string" &&
      value.untrusted_peer_content.length <= 32_000 && !!value.untrusted_peer_content.trim() &&
      !value.untrusted_peer_content.includes("\0");
  } catch {
    return false;
  }
}

function validStart(params, frame) {
  if (!localRequest(params, frame) || !identifier(params.operationId)) return false;
  if (params.action === "poll") return hasFields(params, "hostId", "action", "operationId");
  return params.action === "start" &&
    hasFields(params, "hostId", "action", "operationId", "folder", "model", "effort", "toolOutput") &&
    typeof params.folder === "string" && isAbsolute(params.folder) && !params.folder.includes("\0") &&
    validModel(params.model) && validEffort(params.effort) && peerOutput(params.toolOutput, params.operationId);
}

function validControl(params, frame) {
  if (!localRequest(params, frame) || !identifier(params.conversationId)) return false;
  switch (params.action) {
    case "status":
    case "interrupt":
    case "archive":
    case "unarchive":
      return hasFields(params, "hostId", "action", "conversationId");
    case "model":
      return hasFields(params, "hostId", "action", "conversationId", "value") && validModel(params.value);
    case "effort":
      return hasFields(params, "hostId", "action", "conversationId", "value") && validEffort(params.value);
    default:
      return false;
  }
}

function validRequest(params, frame) {
  return hasFields(params, "hostId", "conversationId", "action") &&
    localRequest(params, frame) && identifier(params.conversationId) &&
    (params.action === "check" || params.action === "warm");
}

function savedDesktopThread(thread, id, archived) {
  return thread?.id === id && thread.ephemeral === false && thread.parentThreadId == null &&
    DESKTOP_SOURCES.has(thread.source) && (thread.originator === "Codex Desktop" ||
      (typeof thread.originator === "string" && /^codex_chatgpt_.*_remote$/.test(thread.originator))) &&
    typeof thread.path === "string" && isAbsolute(thread.path) &&
    typeof thread.cwd === "string" && isAbsolute(thread.cwd) && (archived || thread.canAcceptDirectInput !== false);
}

function registerPrepareThread({ host, ipcClient, assertCurrent }) {
  if (host.getHostId() !== HOST) return () => undefined;

  const preparations = new Map();
  const starts = new Map();
  let disposed = false;

  function available(id) {
    return !disposed && !host.isConversationArchiving(id) && !host.isConversationSuppressedAfterArchive(id);
  }

  function owns(id) {
    return host.getStreamRole(id)?.role === "owner";
  }

  async function readEligibleThread(id, archived = false) {
    const eligible = () => archived ? !disposed && !host.isConversationArchiving(id) : available(id);
    if (!eligible()) return null;
    const { thread } = await host.readThread(id, { includeTurns: false });
    assertCurrent();
    if (!eligible() || !savedDesktopThread(thread, id, archived)) return null;
    // Read the native archived roster without changing the thread's archive state.
    const ids = await host.threadStore.getUnarchivedThreadIds([id], () => undefined);
    assertCurrent();
    return ids.includes(id) !== archived && eligible() ? thread : null;
  }

  async function readActivity(id, thread) {
    switch (thread.status?.type) {
      case "idle":
      case "notLoaded":
        return { activity: "idle" };
      case "active": {
        const { response } = await host.listThreadTurns(id, { limit: 1, itemsView: "notLoaded" });
        assertCurrent();
        const latest = response.data[0];
        return latest?.status === "inProgress" && typeof latest.id === "string" ?
          { activity: "running", turnId: latest.id } : { activity: "running" };
      }
      default:
        return { activity: "unknown" };
    }
  }

  function refusal(reason) {
    return { state: "refused", reason };
  }

  function outcomeUnknown(reason) {
    return { state: "unknown", reason };
  }

  function assertActive() {
    if (disposed) throw Error("Claudex host retired.");
    assertCurrent();
  }

  function startReceipt(result) {
    if (result?.status === "created" && identifier(result.conversationId)) {
      const chatId = result.conversationId;
      const first = result.firstTurn;
      if (first?.status === "accepted" && typeof first.turnId === "string" && first.turnId.length) {
        return { state: "submitted", chatId, turnId: first.turnId };
      }
      const reason = first?.status === "outcome-unknown" ?
        "Codex Desktop created the chat. The first turn's outcome is unknown." :
        "Codex Desktop created the chat, but did not accept the first turn.";
      return { state: "created", chatId, reason };
    }
    if (result?.status === "failed") return refusal("Codex Desktop refused to start a chat.");
    return outcomeUnknown("Codex Desktop did not confirm chat creation.");
  }

  async function start(params, record) {
    const { folder, model, effort, toolOutput } = params;
    try {
      if (!(await stat(folder)).isDirectory()) {
        record.result = refusal("The Codex working folder does not exist.");
        return;
      }
      assertActive();
    } catch {
      record.result = refusal("The working folder or current Codex Desktop host is unavailable.");
      return;
    }

    try {
      const result = await host.startConversation({
        cwd: folder,
        workspaceRoots: [folder],
        collaborationMode: {
          mode: "default",
          settings: { model, reasoning_effort: effort, developer_instructions: null },
        },
        input: [],
        toolOutput,
        useAppServerPermissionDefault: true,
      }, {
        assertRequestCurrent: assertActive,
        // The main-process host has no renderer tool provider. Codex still
        // reads the local app-server configuration, including configured MCP.
        readThreadCreationInputs: async () => ({
          hasDesktopRuntime: false, hasDeveloperInstructions: false,
          usesDesktopMcp: false, isBrowserRuntime: false,
        }),
        onSettled: (settled) => { record.result = startReceipt(settled); },
      });
      record.result ??= startReceipt(result);
    } catch (error) {
      record.result ??= outcomeUnknown("Codex Desktop did not confirm chat creation.");
      host.logger?.debug?.("claudex_start_chat_failed", { safe: {}, sensitive: { error } });
    }
  }

  const removeStart = ipcClient.addRequestHandler(START, validStart, async (frame) => {
    const params = frame?.params;
    if (!validStart(params, frame)) return refusal("Invalid local Codex start request.");
    try {
      assertActive();
    } catch {
      return outcomeUnknown("The Codex Desktop host changed during chat creation.");
    }
    const prior = starts.get(params.operationId);
    if (prior) return prior.result ?? { state: "pending" };
    if (params.action === "poll") return outcomeUnknown("The Codex Desktop start receipt expired.");
    if (starts.size >= MAX_STARTS) return refusal("Too many recent Codex starts. Try again shortly.");

    const record = {
      result: undefined,
      timer: setTimeout(() => starts.delete(params.operationId), RETENTION_MS).unref(),
    };
    starts.set(params.operationId, record);
    void start(params, record);
    return { state: "pending" };
  });

  const removeControl = ipcClient.addRequestHandler(CONTROL, validControl, async (frame) => {
    const params = frame?.params;
    if (!validControl(params, frame)) return refusal("Invalid local Codex control request.");
    const { action, conversationId: id, value } = params;
    let applying = false;
    try {
      assertActive();
      const thread = await readEligibleThread(id, action === "unarchive");
      if (!thread) return refusal("No matching Codex Desktop chat in the required archive scope.");

      if (action === "status") {
        return { state: "applied", chatId: id, ...await readActivity(id, thread) };
      }
      if (action === "interrupt" || action === "archive") {
        const { activity, turnId } = await readActivity(id, thread);
        assertActive();
        if (!available(id)) return refusal("The Codex chat is being archived.");
        if (action === "interrupt") {
          if (!turnId || !owns(id)) return refusal("No loaded active turn could be confirmed for this chat.");
          applying = true;
          const interrupted = await host.interruptConversation(id, "user-stop", turnId);
          return interrupted === turnId ? { state: "applied", chatId: id, turnId } :
            refusal("That Codex turn is no longer active.");
        }
        if (activity !== "idle") return refusal("Interrupt or wait for the active Codex turn before archiving.");
        applying = true;
        await host.archiveConversation(id, { useAppServerPins: false, cleanupWorktree: false, source: "claudex" });
        const active = await host.threadStore.getUnarchivedThreadIds([id], () => undefined);
        assertActive();
        return active.includes(id) ? outcomeUnknown("Codex Desktop did not confirm the archive.") :
          { state: "applied", chatId: id };
      }
      assertActive();
      if (action === "unarchive") {
        applying = true;
        const result = await host.unarchiveConversation(id, { useAppServerPins: false, restorePin: false });
        return result === "unarchived" || result === "alreadyActive" ? { state: "applied", chatId: id } :
          outcomeUnknown("Codex Desktop did not confirm the unarchive.");
      }
      if (!available(id) || !owns(id)) return refusal("The Codex chat has no loaded owner for settings.");
      applying = true;
      const applied = await host.updateThreadSettingsForNextTurn(id, { [action]: value });
      return applied === true ? { state: "applied", chatId: id } :
        refusal("Codex Desktop did not apply the setting.");
    } catch (error) {
      host.logger?.debug?.("claudex_control_chat_failed", { safe: { action }, sensitive: { error } });
      return applying ? outcomeUnknown("Codex Desktop did not confirm the change.") :
        refusal("Codex Desktop could not check this chat.");
    }
  });

  const validBusy = (params, frame) => hasFields(params, "hostId") && localRequest(params, frame);
  const removeBusy = ipcClient.addRequestHandler(BUSY, validBusy, async (frame) => {
    if (!validBusy(frame?.params, frame)) return { state: "unknown" };
    try {
      assertActive();
      const ids = new Set();
      let pending = host.hasPendingConversationResume() ||
        host.requestClient.getPendingRequestCount({ method: "thread/start", retainResponse: true }) > 0 ||
        [...starts.values()].some(record => !record.result);
      function addBusy(id) {
        if (identifier(id)) ids.add(id);
        else pending = true;
      }
      for (const summary of host.getThreadSummaries()) {
        if (summary.threadRuntimeStatus?.type === "active") addBusy(summary.conversationId);
      }
      // The host's work-in-progress report uses runtime state and pending
      // requests, which can become visible before the SQLite history projection.
      for (const chat of host.getCachedConversations()) {
        if (chat.threadRuntimeStatus?.type === "active" || chat.threadGoal?.status === "active" ||
            chat.unconfirmedTurnSubmissions?.some(submission => submission.terminal !== true) ||
            chat.requests.length > 0 || host.turnCoordinator.readMessages(chat.id)?.length > 0 ||
            (chat.resumeState !== "needs_resume" && chat.turns.at(-1)?.status === "inProgress")) {
          addBusy(chat.id);
        }
      }
      if (Object.values(host.turnCoordinator.readState() ?? {}).some(queue => queue?.length > 0) ||
          (ids.size === 0 && host.getHasInProgressLocalConversation())) {
        pending = true;
      }
      return { state: "known", chatIds: [...ids], pending };
    } catch (error) {
      host.logger?.debug?.("claudex_busy_chats_failed", { safe: {}, sensitive: { error } });
      return { state: "unknown" };
    }
  });

  function release(preparation) {
    preparation.lease?.[Symbol.dispose]();
    preparation.lease = undefined;
  }

  function forget(id, preparation) {
    clearTimeout(preparation.timer);
    preparations.delete(id);
    release(preparation);
  }

  async function prepare(id, preparation) {
    try {
      const thread = await readEligibleThread(id);
      if (!thread || preparations.get(id) !== preparation) return;
      preparation.lease = host.retainActiveConversation(id);
      while (!owns(id)) {
        const result = await host.resumeConversationForUnavailableOwner({
          conversationId: id,
          model: null,
          reasoningEffort: null,
          workspaceRoots: [thread.cwd],
          collaborationMode: host.getConversation(id)?.latestCollaborationMode ?? null,
        });
        if (result?.status === "ready") break;
        if (result?.status !== "not-ready" || result.reason !== "execution-config-loading") {
          host.logger?.debug?.("claudex_prepare_thread_not_ready", {
            safe: { status: result?.status, reason: result?.reason },
          });
          return;
        }
        // Resume only. The relay cannot submit peer output until it rediscovers an owner.
        await new Promise(resolve => setTimeout(resolve, EXECUTION_CONFIG_RETRY_MS));
        assertCurrent();
        if (preparations.get(id) !== preparation || !available(id)) return;
      }
      assertCurrent();
      if (preparations.get(id) === preparation && available(id) && owns(id)) {
        preparation.state = "ready";
      }
    } catch (error) {
      host.logger?.debug?.("claudex_prepare_thread_failed", { safe: {}, sensitive: { error } });
    } finally {
      if (preparation.state !== "ready") {
        preparation.state = "unavailable";
        release(preparation);
      }
    }
  }

  const remove = ipcClient.addRequestHandler(METHOD, validRequest, async (frame) => {
    const params = frame?.params;
    if (!validRequest(params, frame)) return { state: "unavailable" };
    const id = params.conversationId;
    try {
      assertCurrent();
      if (!available(id)) return { state: "unavailable" };

      if (params.action === "check") {
        if (!await readEligibleThread(id)) return { state: "unavailable" };
        if (owns(id)) return { state: "ready" };
        const state = preparations.get(id)?.state;
        return { state: state === "warming" || state === "unavailable" ? state : "available" };
      }

      const preparation = preparations.get(id);
      if (preparation) {
        if (preparation.state !== "ready" || owns(id)) return { state: preparation.state };
        forget(id, preparation);
      }

      const pending = {
        state: "warming",
        timer: setTimeout(() => forget(id, pending), RETENTION_MS).unref(),
      };
      preparations.set(id, pending);
      void prepare(id, pending);
      return { state: "warming" };
    } catch {
      return { state: "unavailable" };
    }
  });

  return () => {
    if (disposed) return;
    disposed = true;
    remove();
    removeStart();
    removeControl();
    removeBusy();
    for (const { timer } of starts.values()) clearTimeout(timer);
    starts.clear();
    for (const [id, preparation] of preparations) forget(id, preparation);
  };
}

module.exports = { registerPrepareThread };
