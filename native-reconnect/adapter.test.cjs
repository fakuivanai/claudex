'use strict';

const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { handleRequest } = require('./adapter.cjs');

const SESSION = 'local_' + randomUUID();
const FLAG = '--thread-bridge-reconnect';
const EXECUTABLE = '/fixture/claude-desktop';

async function writePrivate(file, value) {
  await fs.writeFile(file, JSON.stringify(value), { mode: 0o600, flag: 'wx' });
}

async function fixture(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'native-reconnect-test-'));
  t.after(() => fs.rm(directory, { force: true, recursive: true }));
  const marker = path.join(directory, 'profile');
  const configPath = path.join(directory, 'config.json');
  const config = {
    allowed_session_ids: [SESSION],
    account_id: 'fixture-account',
    org_id: 'fixture-org',
    profile: 'fixture-profile',
    profile_marker: marker,
  };
  await fs.writeFile(marker, config.profile + '\n', { mode: 0o600 });
  await writePrivate(configPath, config);
  const env = { THREAD_BRIDGE_CLAUDE_RECONNECT_DIR: directory };
  const record = {
    sessionId: SESSION,
    cliSessionId: randomUUID(),
    isArchived: false,
    isRunning: false,
    homeTenancy: { accountId: config.account_id, orgId: config.org_id },
    backend: { kind: 'local' },
    query: {},
    inputStream: {},
    cwd: '/fixture/workspace',
    worktreePath: '/fixture/worktree',
  };
  const calls = [];
  const manager = {
    currentAccountId: config.account_id,
    currentOrgId: config.org_id,
    sessions: new Map([[record.sessionId, record]]),
    remoteControlLifecycle: {
      async maybeAutoEnableRemoteControl(...args) {
        calls.push(args);
        return 'connected';
      },
    },
  };
  const nonce = randomUUID();
  const requestFile = path.join(directory, nonce + '.request.json');
  const replyFile = path.join(directory, nonce + '.reply.json');
  const args = [EXECUTABLE, FLAG + '=' + nonce];
  const reply = async () => JSON.parse(await fs.readFile(replyFile, 'utf8'));
  const request = payload => writePrivate(requestFile, payload ?? { session_id: SESSION });
  return { directory, env, manager, record, calls, args, nonce, requestFile, replyFile, request, reply, configPath, marker };
}

test('ordinary launches fall through and malformed reserved commands do not', async () => {
  assert.equal(await handleRequest([EXECUTABLE, 'claude://code/new'], null, {}), false);
  assert.equal(await handleRequest([EXECUTABLE, FLAG], null, {}), true);
  assert.equal(await handleRequest([EXECUTABLE, FLAG + '=../../config.json'], null, {}), true);
  assert.equal(await handleRequest([EXECUTABLE, FLAG + '=' + randomUUID(), 'claude://code/new'], null, {}), true);
});

test('claims a valid request once and reports only the native outcome', async t => {
  const f = await fixture(t);
  f.record.steeredByRemoteClient = true;
  await f.request();
  let release;
  let entered;
  const pause = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  f.manager.remoteControlLifecycle.maybeAutoEnableRemoteControl = async (...args) => {
    f.calls.push(args);
    entered();
    await pause;
    return 'deferred';
  };
  const first = handleRequest(f.args, f.manager, f.env);
  await started;
  assert.equal(await handleRequest(f.args, f.manager, f.env), true);
  await assert.rejects(fs.stat(f.replyFile), { code: 'ENOENT' });
  release();
  assert.equal(await first, true);

  assert.deepEqual(f.calls, [[f.record, '/fixture/worktree', 'local_reconnect']]);
  assert.deepEqual(await f.reply(), { status: 'deferred' });
  assert.equal((await fs.stat(f.replyFile)).mode & 0o777, 0o600);
  await assert.rejects(fs.stat(f.requestFile), { code: 'ENOENT' });
  assert.equal((await fs.stat(path.join(f.directory, f.nonce + '.claim'))).size, 0);

  await fs.unlink(f.replyFile);
  await f.request();
  assert.equal(await handleRequest(f.args, f.manager, f.env), true);
  assert.equal(f.calls.length, 1);
  await assert.rejects(fs.stat(f.replyFile), { code: 'ENOENT' });
});

test('refuses invalid request, profile, and account before calling the app', async t => {
  const cases = [
    ['invalid_request', async f => f.request({ session_id: SESSION, prompt: 'fixture' })],
    ['not_allowlisted', async f => f.request({ session_id: 'local_' + randomUUID() })],
    ['unconfigured', async f => { await fs.chmod(f.configPath, 0o644); await f.request(); }],
    ['profile_mismatch', async f => { await fs.writeFile(f.marker, 'different'); await f.request(); }],
    ['account_mismatch', async f => { f.manager.currentAccountId = 'different'; await f.request(); }],
    ['account_mismatch', async f => { f.manager.currentOrgId = 'different'; await f.request(); }],
    ['session_account_mismatch', async f => { f.record.homeTenancy.orgId = 'different'; await f.request(); }],
  ];
  for (const [reason, prepare] of cases) {
    await t.test(reason, async st => {
      const f = await fixture(st);
      await prepare(f);
      assert.equal(await handleRequest(f.args, f.manager, f.env), true);
      assert.deepEqual(f.calls, []);
      assert.deepEqual(await f.reply(), { status: 'refused', reason });
    });
  }
});

test('refuses saved records without an eligible warm local worker', async t => {
  const cases = [
    ['unsaved_or_archived', { isArchived: true }],
    ['unsaved_or_archived', { sessionId: 'local_' + randomUUID() }],
    ['requires_user', { stoppedUntilPersonSends: true }],
    ['requires_user', { importedFrom: 'fixture', resumeConfirmed: false }],
    ['remote_owner', { remoteControlSpawn: {} }],
    ['remote_owner', { remoteControlDescendant: true }],
    ['remote_owner', { backend: { kind: 'ssh' } }],
    ['worker_not_ready', { query: null }],
    ['worker_not_ready', { inputStream: null }],
    ['worker_busy', { isRunning: true }],
  ];
  for (const [reason, record] of cases) {
    await t.test(reason + ' ' + Object.keys(record)[0], async st => {
      const f = await fixture(st);
      Object.assign(f.record, record);
      await f.request();
      await handleRequest(f.args, f.manager, f.env);
      assert.deepEqual(f.calls, []);
      assert.deepEqual(await f.reply(), { status: 'refused', reason });
    });
  }
});

test('accepts an absent organization for the pinned standalone account', async t => {
  const f = await fixture(t);
  const config = JSON.parse(await fs.readFile(f.configPath, 'utf8'));
  config.org_id = null;
  await fs.writeFile(f.configPath, JSON.stringify(config));
  delete f.manager.currentOrgId;
  delete f.record.homeTenancy.orgId;
  delete f.record.worktreePath;
  await f.request();
  await handleRequest(f.args, f.manager, f.env);
  assert.deepEqual(f.calls, [[f.record, '/fixture/workspace', 'local_reconnect']]);
  assert.deepEqual(await f.reply(), { status: 'connected' });
});

test('does not follow symlinks or process permissive local files', async t => {
  await t.test('request symlink', async st => {
    const f = await fixture(st);
    const original = path.join(f.directory, 'fixture.json');
    await writePrivate(original, { session_id: SESSION });
    await fs.symlink(original, f.requestFile);
    await handleRequest(f.args, f.manager, f.env);
    assert.deepEqual(f.calls, []);
    await assert.rejects(fs.stat(f.replyFile), { code: 'ENOENT' });
  });
  await t.test('permissive request', async st => {
    const f = await fixture(st);
    await f.request();
    await fs.chmod(f.requestFile, 0o644);
    await handleRequest(f.args, f.manager, f.env);
    assert.deepEqual(f.calls, []);
    await assert.rejects(fs.stat(f.replyFile), { code: 'ENOENT' });
  });
  await t.test('permissive endpoint', async st => {
    const f = await fixture(st);
    await f.request();
    await fs.chmod(f.directory, 0o755);
    await handleRequest(f.args, f.manager, f.env);
    assert.deepEqual(f.calls, []);
    await assert.rejects(fs.stat(f.replyFile), { code: 'ENOENT' });
  });
});

test('replies do not expose native failures or claim unattested success', async t => {
  await t.test('raw exception', async st => {
    const f = await fixture(st);
    f.manager.remoteControlLifecycle.maybeAutoEnableRemoteControl = async () => {
      throw new Error('fixture-private-account-material');
    };
    await f.request();
    await handleRequest(f.args, f.manager, f.env);
    assert.deepEqual(await f.reply(), { status: 'refused', reason: 'native_error' });
  });
  await t.test('unexpected outcome', async st => {
    const f = await fixture(st);
    f.manager.remoteControlLifecycle.maybeAutoEnableRemoteControl = async () => ({ authorized: true });
    await f.request();
    await handleRequest(f.args, f.manager, f.env);
    assert.deepEqual(await f.reply(), { status: 'refused', reason: 'unknown_native_result' });
  });
});
