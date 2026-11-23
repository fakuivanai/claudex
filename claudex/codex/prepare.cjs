"use strict";

const { isAbsolute } = require("node:path");

const METHOD = "claudex-prepare-thread";
const HOST = "local";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const DESKTOP_SOURCES = new Set(["vscode", "exec", "appServer", "app_server"]);
// Native idle-thread eviction can otherwise race the client's next request.
const RETENTION_MS = 60_000;

function validRequest(params, frame) {
  return !!params && typeof params === "object" && Object.keys(params).length === 3 &&
    (frame?.hostId == null || frame.hostId === HOST) && params.hostId === HOST &&
    typeof params.conversationId === "string" && UUID.test(params.conversationId) &&
    (params.action === "check" || params.action === "warm");
}

function savedDesktopThread(thread, id) {
  return thread?.id === id && thread.ephemeral === false && thread.parentThreadId == null &&
    DESKTOP_SOURCES.has(thread.source) && (thread.originator === "Codex Desktop" ||
      (typeof thread.originator === "string" && /^codex_chatgpt_.*_remote$/.test(thread.originator))) &&
    typeof thread.path === "string" && isAbsolute(thread.path) &&
    typeof thread.cwd === "string" && isAbsolute(thread.cwd) && thread.canAcceptDirectInput !== false;
}

function registerPrepareThread({ host, ipcClient, assertCurrent }) {
  if (host.getHostId() !== HOST) return () => undefined;

  const preparations = new Map();
  let disposed = false;

  function available(id) {
    return !disposed && !host.isConversationArchiving(id) && !host.isConversationSuppressedAfterArchive(id);
  }

  function owns(id) {
    return host.getStreamRole(id)?.role === "owner";
  }

  async function readEligibleThread(id) {
    if (!available(id)) return null;
    const { thread } = await host.readThread(id, { includeTurns: false });
    assertCurrent();
    if (!available(id) || !savedDesktopThread(thread, id)) return null;
    // Read the native archived roster without changing the thread's archive state.
    const ids = await host.threadStore.getUnarchivedThreadIds([id], () => undefined);
    assertCurrent();
    return ids.includes(id) && available(id) ? thread : null;
  }

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
      if (!owns(id)) {
        const result = await host.resumeConversationForUnavailableOwner({
          conversationId: id,
          model: null,
          reasoningEffort: null,
          workspaceRoots: [thread.cwd],
          collaborationMode: host.getConversation(id)?.latestCollaborationMode ?? null,
        });
        if (result?.status !== "ready") return;
      }
      assertCurrent();
      if (preparations.get(id) === preparation && available(id) && owns(id)) {
        preparation.state = "ready";
      }
    } catch {
      // Invalid or retired hosts cannot prepare a thread.
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
    for (const [id, preparation] of preparations) forget(id, preparation);
  };
}

module.exports = { registerPrepareThread };
