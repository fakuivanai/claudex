"use strict";

const { once } = require("node:events");
const { lstatSync, mkdirSync, unlinkSync } = require("node:fs");
const net = require("node:net");
const path = require("node:path");

// The Python tests check these shared wire bounds. A request holds one chat
// ID and small options. A reply can hold a full Desktop roster.
const MAX_REQUEST_BYTES = 4096;
const MAX_RESPONSE_BYTES = 524288;
// Bound the number of in-flight connections, including queued requests.
// The local request frame is tiny, so two seconds also bounds stalled writers.
const MAX_CONNECTIONS = 8;
const REQUEST_TIMEOUT_MS = 2000;
const EFFORTS = new Set(["low", "medium", "high", "xhigh", "max"]);
const endpoints = new Set();
const LOCAL_CHAT_ID = /^local_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
const utf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

function exactObject(value, ...keys) {
  return value !== null && typeof value === "object" && !Array.isArray(value) &&
    Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}

function optionToken(value, max) {
  return typeof value === "string" && value.length > 0 && value.length <= max &&
    value.trim() === value && !/[\u0000-\u001f\u007f]/u.test(value);
}

function parseRequest(bytes) {
  const frame = utf8.decode(bytes);
  if (!frame.endsWith("\n") || frame.indexOf("\n") !== frame.length - 1) {
    throw new Error("Expected one newline-terminated JSON request.");
  }
  const request = JSON.parse(frame);
  if (!exactObject(request, "tool", "input")) {
    throw new Error("Invalid request envelope.");
  }
  const { tool, input } = request;
  if (tool === "getSessionList" && exactObject(input)) return request;
  const id = input?.chat_id;
  if (typeof id !== "string" || id.length !== 42 || !LOCAL_CHAT_ID.test(id)) {
    throw new Error("Invalid Desktop chat ID.");
  }
  switch (tool) {
    case "getSession":
    case "warmSession":
    case "archive":
    case "unarchive":
    case "stop":
    case "interrupt":
      if (exactObject(input, "chat_id")) return request;
      break;
    case "toggleRemoteControl":
    case "setFastMode":
      if (exactObject(input, "chat_id", "enabled") && typeof input.enabled === "boolean") return request;
      break;
    case "setModel":
      if (exactObject(input, "chat_id", "model") && optionToken(input.model, 128)) return request;
      break;
    case "setEffort":
      if (exactObject(input, "chat_id", "effort") &&
          (input.effort === null || EFFORTS.has(input.effort))) return request;
      break;
    case "setPermissionMode":
      if (exactObject(input, "chat_id", "mode") &&
          optionToken(input.mode, 32)) {
        return request;
      }
      break;
  }
  throw new Error("Unsupported tool or invalid input.");
}

function error(reason) {
  return { status: "error", reason };
}

function reply(socket, response) {
  let wire;
  try {
    const success = response?.status === "ok" && exactObject(response, "status", "result") &&
      response.result !== undefined;
    const refusal = (response?.status === "refused" || response?.status === "error") &&
      exactObject(response, "status", "reason") && typeof response.reason === "string";
    if (!success && !refusal) throw new Error("Invalid handler response.");
    wire = JSON.stringify(response) + "\n";
    if (Buffer.byteLength(wire) > MAX_RESPONSE_BYTES) {
      wire = JSON.stringify(error("Desktop response exceeds the size limit.")) + "\n";
    }
  } catch {
    wire = JSON.stringify(error("Desktop handler returned an invalid response.")) + "\n";
  }
  socket.end(wire);
}

function prepareEndpoint(directory, endpoint) {
  // The graphical login provides a private XDG_RUNTIME_DIR. Electron holds
  // its single-instance lock before starting this listener.
  try {
    mkdirSync(directory, { mode: 0o700 });
  } catch (error) {
    if (error.code !== "EEXIST") throw error;
    if (!lstatSync(directory).isDirectory()) throw new Error("The Desktop socket path is not a directory.");
  }
  try {
    if (!lstatSync(endpoint).isSocket()) throw new Error("The Desktop socket path contains a non-socket.");
    unlinkSync(endpoint);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
}

async function startDesktopSocket(dispatch, {
  runtimeDirectory = process.env.XDG_RUNTIME_DIR, capture = () => undefined,
} = {}) {
  if (typeof runtimeDirectory !== "string" || !path.isAbsolute(runtimeDirectory)) {
    throw new Error("Desktop socket requires an absolute XDG_RUNTIME_DIR.");
  }
  const directory = path.join(runtimeDirectory, "claudex");
  const endpoint = path.join(directory, "desktop.sock");
  if (endpoints.has(endpoint)) throw new Error("The Desktop socket already exists in this process.");
  prepareEndpoint(directory, endpoint);

  const sockets = new Set();
  const queue = [];
  let active = null;
  async function dispatchQueued() {
    if (active) return;
    while (queue.length) {
      const { socket, request, scope } = queue.shift();
      active = socket;
      try {
        reply(socket, await dispatch(request, scope));
      } catch {
        reply(socket, error("Desktop handler failed."));
      } finally {
        active = null;
        if (socket.destroyed) sockets.delete(socket);
      }
    }
  }

  const server = net.createServer({ allowHalfOpen: true }, (socket) => {
    socket.on("error", () => socket.destroy());
    if (sockets.size >= MAX_CONNECTIONS) {
      reply(socket, error("Too many desktop socket connections."));
      socket.destroySoon();
      return;
    }
    sockets.add(socket);
    const chunks = [];
    let length = 0;
    let ended = false;
    const timeout = setTimeout(() => reject("Timed out reading the desktop request."), REQUEST_TIMEOUT_MS);
    function reject(reason) {
      if (ended) return;
      ended = true;
      clearTimeout(timeout);
      reply(socket, error(reason));
      socket.destroySoon();
    }
    socket.on("close", () => {
      clearTimeout(timeout);
      if (socket !== active) sockets.delete(socket);
      const waiting = queue.findIndex((entry) => entry.socket === socket);
      if (waiting !== -1) queue.splice(waiting, 1);
    });
    socket.on("data", (chunk) => {
      if (ended) return;
      length += chunk.length;
      if (length > MAX_REQUEST_BYTES) return reject("Desktop request exceeds the size limit.");
      chunks.push(chunk);
    });
    socket.on("end", () => {
      if (ended) return;
      ended = true;
      clearTimeout(timeout);
      try {
        const request = parseRequest(Buffer.concat(chunks, length));
        queue.push({ socket, request, scope: capture() });
        void dispatchQueued();
      } catch {
        reply(socket, error("Invalid desktop request."));
      }
    });
  });
  // Once startup completes, keep a socket error from terminating Electron.
  server.on("error", () => {});
  endpoints.add(endpoint);
  try {
    // Unix sockets bind with mode 0777 before umask. Also mask owner execute
    // to keep the existing mode 0600 contract. The Unix bind is synchronous.
    const previous = process.umask(0o177);
    try {
      server.listen(endpoint);
    } finally {
      process.umask(previous);
    }
    await once(server, "listening");
  } catch (error) {
    endpoints.delete(endpoint);
    throw error;
  }

  let closing;
  return {
    close() {
      closing ??= new Promise((resolve, reject) => {
        queue.length = 0;
        for (const socket of sockets) socket.destroy();
        server.close((cause) => {
          endpoints.delete(endpoint);
          if (cause) reject(cause);
          else resolve();
        });
      });
      return closing;
    },
  };
}

module.exports = { startDesktopSocket, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES };
