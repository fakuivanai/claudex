"use strict";

const { randomBytes } = require("node:crypto");
const { once } = require("node:events");
const { lstatSync, unlinkSync } = require("node:fs");
const fs = require("node:fs/promises");
const net = require("node:net");
const path = require("node:path");

const MAX_REQUEST_BYTES = 4096;
const MAX_RESPONSE_BYTES = 512 * 1024;
const MAX_CONNECTIONS = 8;
const REQUEST_TIMEOUT_MS = 2000;
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
      if (exactObject(input, "chat_id", "model") && optionToken(input.model, 256)) return request;
      break;
    case "setEffort":
      if (exactObject(input, "chat_id", "effort") &&
          (input.effort === null || optionToken(input.effort, 32))) return request;
      break;
    case "setPermissionMode":
      if (exactObject(input, "chat_id", "mode") &&
          ["plan", "default", "dontAsk", "acceptEdits", "auto", "bypassPermissions"].includes(input.mode)) {
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

async function requirePrivateDirectory(directory) {
  const info = await fs.lstat(directory);
  if (!info.isDirectory() || info.uid !== process.geteuid() || (info.mode & 0o7777) !== 0o700) {
    throw new Error("Desktop socket requires a directory owned by this user with mode 0700.");
  }
}

async function unlinkPublishedSocket(endpoint, published) {
  try {
    const current = await fs.lstat(endpoint);
    if (current.dev === published.dev && current.ino === published.ino) {
      await fs.unlink(endpoint);
    }
  } catch (cause) {
    if (cause.code !== "ENOENT") throw cause;
  }
}

function socketIsListening(endpoint) {
  return new Promise((resolve, reject) => {
    const probe = net.createConnection(endpoint);
    probe.setTimeout(1000);
    probe.once("connect", () => {
      probe.destroy();
      resolve(true);
    });
    probe.once("timeout", () => {
      probe.destroy();
      reject(new Error("Could not determine whether the Desktop socket is still in use."));
    });
    probe.once("error", (cause) => {
      if (cause.code === "ECONNREFUSED") resolve(false);
      else reject(cause);
    });
  });
}

async function removeStaleEndpoint(directory, endpoint) {
  let existing;
  try {
    existing = await fs.lstat(endpoint);
  } catch (cause) {
    if (cause.code === "ENOENT") return;
    throw cause;
  }
  if (!existing.isSocket() || existing.uid !== process.geteuid() ||
      (existing.mode & 0o7777) !== 0o600 || await socketIsListening(endpoint)) {
    throw new Error("The desktop socket endpoint already exists.");
  }
  await unlinkPublishedSocket(endpoint, existing);
  for (const name of await fs.readdir(directory)) {
    if (/^\.sock-[a-f0-9]{12}$/u.test(name)) {
      await unlinkPublishedSocket(path.join(directory, name), existing);
    }
  }
}

async function startDesktopSocket(dispatch, { runtimeDirectory = process.env.XDG_RUNTIME_DIR } = {}) {
  if (typeof dispatch !== "function") throw new TypeError("A native handler is required.");
  if (typeof runtimeDirectory !== "string" || !path.isAbsolute(runtimeDirectory)) {
    throw new Error("Desktop socket requires an absolute XDG_RUNTIME_DIR.");
  }
  await requirePrivateDirectory(runtimeDirectory);
  const directory = path.join(runtimeDirectory, "claudex");
  try {
    await fs.mkdir(directory, { mode: 0o700 });
  } catch (cause) {
    if (cause.code !== "EEXIST") throw cause;
  }
  await requirePrivateDirectory(directory);
  const endpoint = path.join(directory, "desktop.sock");
  const listenerPath = path.join(directory, `.sock-${randomBytes(6).toString("hex")}`);
  await removeStaleEndpoint(directory, endpoint);
  if (Buffer.byteLength(listenerPath) > 107) {
    throw new Error("Desktop socket path is too long.");
  }

  const sockets = new Set();
  let busy = false;
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
      sockets.delete(socket);
    });
    socket.on("data", (chunk) => {
      if (ended) return;
      length += chunk.length;
      if (length > MAX_REQUEST_BYTES) return reject("Desktop request exceeds the size limit.");
      chunks.push(chunk);
    });
    socket.on("end", async () => {
      if (ended) return;
      ended = true;
      clearTimeout(timeout);
      let request;
      try {
        request = parseRequest(Buffer.concat(chunks, length));
      } catch {
        reply(socket, error("Invalid desktop request."));
        return;
      }
      if (busy) {
        reply(socket, error("The desktop handler is busy."));
        return;
      }
      busy = true;
      try {
        reply(socket, await dispatch(request));
      } catch {
        reply(socket, error("Desktop handler failed."));
      } finally {
        busy = false;
      }
    });
  });
  // The opening promise reports bind failures. A later accept error cannot exit the host app.
  server.on("error", () => {});

  function stopListening() {
    for (const socket of sockets) socket.destroy();
    return new Promise((resolve, reject) => server.close((cause) => {
      if (cause) reject(cause);
      else resolve();
    }));
  }

  server.listen(listenerPath);
  await once(server, "listening");
  let published;
  try {
    await fs.chmod(listenerPath, 0o600);
    published = await fs.lstat(listenerPath);
    // Publish only after setting the permissions. Atomic linking never displaces another listener.
    await fs.link(listenerPath, endpoint);
  } catch (cause) {
    await stopListening();
    if (cause.code === "EEXIST") throw new Error("The desktop socket endpoint already exists.");
    throw cause;
  }
  // Electron does not await before-quit listeners. The exit hook covers
  // graceful exits where asynchronous shutdown cannot remove the endpoint.
  const removeOnExit = () => {
    for (const pathname of [endpoint, listenerPath]) {
      try {
        const current = lstatSync(pathname);
        if (current.dev === published.dev && current.ino === published.ino) unlinkSync(pathname);
      } catch {
        // An abrupt exit is covered by the next startup's liveness check.
      }
    }
  };
  process.once("exit", removeOnExit);
  let closing;
  return {
    close() {
      closing ??= (async () => {
        try {
          await unlinkPublishedSocket(endpoint, published);
        } finally {
          await stopListening();
          process.off("exit", removeOnExit);
        }
      })();
      return closing;
    },
  };
}

module.exports = { startDesktopSocket };
