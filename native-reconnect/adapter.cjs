'use strict';

const { constants } = require('node:fs');
const fs = require('node:fs/promises');
const path = require('node:path');

const FLAG = '--thread-bridge-reconnect';
const DIRECTORY_ENV = 'THREAD_BRIDGE_CLAUDE_RECONNECT_DIR';
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/u;
const LOCAL_ID = /^local_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/u;
const READ_FLAGS = constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK;
const CREATE_FLAGS = constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW;
const NATIVE_RESULTS = new Set(['connected', 'deferred', 'failed', 'not_run', 'refused']);

function plainObjectWithKeys(value, keys) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}

function absolutePath(value) {
  return typeof value === 'string' && !value.includes('\0') && path.isAbsolute(value)
    && path.normalize(value) === value;
}

function isId(value, pattern) {
  // ECMAScript's end anchor can match before a final line break.
  return typeof value === 'string' && value.trim() === value && pattern.test(value);
}

function privateMode(stat, mode, uid) {
  return stat.uid === uid && (stat.mode & 0o7777) === mode;
}

async function readOwnedFile(file, uid, maxBytes, privateOnly = true) {
  const handle = await fs.open(file, READ_FLAGS);
  try {
    const stat = await handle.stat();
    if (!stat.isFile() || stat.nlink !== 1 || stat.size > maxBytes || stat.uid !== uid
      || (privateOnly ? !privateMode(stat, 0o600, uid) : (stat.mode & 0o7022) !== 0)) {
      throw new Error('Unexpected local file metadata');
    }
    const bytes = Buffer.alloc(maxBytes + 1);
    const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0);
    if (bytesRead !== stat.size) throw new Error('Local file changed while reading');
    return bytes.subarray(0, bytesRead).toString('utf8');
  } finally {
    await handle.close();
  }
}

function readJson(file, uid, maxBytes) {
  return readOwnedFile(file, uid, maxBytes).then(JSON.parse);
}

function nonemptyLabel(value) {
  return typeof value === 'string' && value.trim() === value && value.length > 0
    && value.length <= 128 && !/[\x00-\x1f\x7f]/u.test(value);
}

function configured(config) {
  return plainObjectWithKeys(config, ['allowed_session_ids', 'account_id', 'org_id', 'profile', 'profile_marker'])
    && Array.isArray(config.allowed_session_ids) && config.allowed_session_ids.length > 0
    && config.allowed_session_ids.every(id => isId(id, LOCAL_ID))
    && nonemptyLabel(config.account_id) && (config.org_id === null || nonemptyLabel(config.org_id))
    && nonemptyLabel(config.profile) && absolutePath(config.profile_marker);
}

function refusal(reason) {
  return { status: 'refused', reason };
}

async function nativeReconnect(request, manager, directory, uid) {
  if (!plainObjectWithKeys(request, ['session_id']) || !isId(request.session_id, LOCAL_ID)) {
    return refusal('invalid_request');
  }

  let config;
  let profile;
  try {
    config = await readJson(path.join(directory, 'config.json'), uid, 16_384);
    if (!configured(config)) return refusal('unconfigured');
    profile = (await readOwnedFile(config.profile_marker, uid, 512, false)).trim();
  } catch {
    return refusal('unconfigured');
  }
  const id = request.session_id;
  if (!config.allowed_session_ids.includes(id)) return refusal('not_allowlisted');
  if (profile !== config.profile) return refusal('profile_mismatch');
  if (manager?.appQuitting === true) return refusal('app_quitting');

  // These fields are those used by the pinned Desktop LocalSessionManager.
  if (typeof manager?.sessions?.get !== 'function'
    || typeof manager?.remoteControlLifecycle?.maybeAutoEnableRemoteControl !== 'function') {
    return refusal('manager_unavailable');
  }
  if (manager.currentAccountId !== config.account_id || (manager.currentOrgId ?? null) !== config.org_id) {
    return refusal('account_mismatch');
  }
  const record = manager.sessions.get(id);
  if (record?.sessionId !== id || record.isArchived !== false) return refusal('unsaved_or_archived');
  if (record.homeTenancy?.accountId !== config.account_id || (record.homeTenancy?.orgId ?? null) !== config.org_id) {
    return refusal('session_account_mismatch');
  }
  if (record.backend?.kind !== 'local' || record.backend.remoteTarget !== undefined
    || record.remoteControlSpawn !== undefined || record.remoteControlDescendant === true) {
    return refusal('remote_owner');
  }
  if (record.stoppedUntilPersonSends || (record.importedFrom && !record.resumeConfirmed)) {
    return refusal('requires_user');
  }
  if (!record.query || !record.inputStream || !isId(record.cliSessionId, UUID)) {
    return refusal('worker_not_ready');
  }
  if (record.isRunning !== false || record.isStopping === true) return refusal('worker_busy');

  const cwd = record.worktreePath || record.cwd;
  if (!absolutePath(cwd)) return refusal('worker_not_ready');
  try {
    const status = await manager.remoteControlLifecycle.maybeAutoEnableRemoteControl(record, cwd, 'local_reconnect');
    return NATIVE_RESULTS.has(status) ? { status } : refusal('unknown_native_result');
  } catch {
    return refusal('native_error');
  }
}

async function publishReply(directory, nonce, reply) {
  const partial = path.join(directory, nonce + '.reply.tmp');
  const destination = path.join(directory, nonce + '.reply.json');
  const handle = await fs.open(partial, CREATE_FLAGS, 0o600);
  try {
    try {
      await handle.writeFile(JSON.stringify(reply) + '\n', 'utf8');
    } finally {
      await handle.close();
    }
    // Publish a complete record without replacing a pre-existing reply.
    await fs.link(partial, destination);
  } finally {
    await fs.unlink(partial);
  }
}

async function processRequest(nonce, manager, env) {
  const endpoint = env[DIRECTORY_ENV];
  if (!absolutePath(endpoint) || process.platform !== 'linux') return;
  const uid = process.getuid();
  const handle = await fs.open(endpoint, READ_FLAGS | constants.O_DIRECTORY);
  try {
    const metadata = await handle.stat();
    const directory = '/proc/self/fd/' + handle.fd;
    if (!privateMode(metadata, 0o700, uid) || await fs.realpath(directory) !== endpoint) return;

    const file = path.join(directory, nonce + '.request.json');
    // Reading precedes claiming so an unsafe file creates no reply or claim.
    const encoded = await readOwnedFile(file, uid, 1024);
    const claim = await fs.open(path.join(directory, nonce + '.claim'), CREATE_FLAGS, 0o600);
    await claim.close();
    // Keep the empty claim for the lifetime of the private endpoint. It keeps
    // a repeated invocation from performing a second action after a timeout.
    const existingReply = await fs.lstat(path.join(directory, nonce + '.reply.json')).catch(error => {
      if (error.code !== 'ENOENT') throw error;
      return null;
    });
    if (existingReply) return;
    await fs.unlink(file);
    let request;
    try {
      request = JSON.parse(encoded);
    } catch {
      request = null;
    }
    const reply = await nativeReconnect(request, manager, directory, uid);
    await publishReply(directory, nonce, reply);
  } finally {
    await handle.close();
  }
}

async function handleRequest(args, manager, env = process.env) {
  if (!Array.isArray(args) || !args.some(arg => (
    typeof arg === 'string' && (arg === FLAG || arg.startsWith(FLAG + '='))
  ))) return false;

  if (args.length !== 2 || !absolutePath(args[0]) || typeof args[1] !== 'string'
    || !args[1].startsWith(FLAG + '=')) return true;
  const nonce = args[1].slice(FLAG.length + 1);
  if (!isId(nonce, UUID)) return true;

  try {
    await processRequest(nonce, manager, env);
  } catch {
    // A rejected reserved command must not fall through to existing launch
    // behavior or leak local paths, filesystem details or account information.
  }
  return true;
}

module.exports = { handleRequest };
