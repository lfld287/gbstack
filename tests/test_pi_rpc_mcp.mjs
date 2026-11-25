import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JsonlDecoder, truncateUtf8 } from '../scripts/mcp/protocol.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..');
const SERVER_PATH = path.join(REPO_ROOT, 'scripts', 'mcp', 'pi-rpc-server.mjs');
const LAUNCHER_PATH = path.join(REPO_ROOT, 'scripts', 'pi-rpc-mcp.sh');
const MCP_CONFIG_PATH = path.join(REPO_ROOT, 'mcp_config.json');

function makeTempDir(prefix = 'pi-rpc-test-') {
  return mkdtempSync(path.join(tmpdir(), prefix));
}

function initGitRepo(dir) {
  const env = { ...process.env, GIT_OPTIONAL_LOCKS: '0' };
  const run = (args) => {
    const res = spawnSync('git', ['-C', dir, ...args], { env, encoding: 'utf8' });
    assert.equal(res.status, 0, `git ${args.join(' ')} failed: ${res.stderr}`);
    return res.stdout.trim();
  };
  run(['init', '-q']);
  run(['config', 'user.email', 'test@example.com']);
  run(['config', 'user.name', 'Test User']);
  writeFileSync(path.join(dir, 'README.md'), 'hello\n');
  run(['add', 'README.md']);
  run(['commit', '-q', '-m', 'init']);
  return run(['rev-parse', 'HEAD']);
}

function createFakePiStub(dir) {
  const stubPath = path.join(dir, 'fake-pi');
  const script = `#!/usr/bin/env node
import { writeFileSync, appendFileSync } from 'node:fs';
import path from 'node:path';

const args = process.argv.slice(2);
if (process.env.FAKE_PI_ARGS_LOG) {
  appendFileSync(process.env.FAKE_PI_ARGS_LOG, JSON.stringify({ pid: process.pid, args }) + '\\n');
}

// Spawn a child sleep process when asked so we can verify process-group cleanup
let bgChild = null;
import { spawn } from 'node:child_process';

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buf += chunk;
  let idx;
  while ((idx = buf.indexOf('\\n')) !== -1) {
    let line = buf.slice(0, idx);
    buf = buf.slice(idx + 1);
    if (line.endsWith('\\r')) line = line.slice(0, -1);
    if (!line.trim()) continue;
    handleCmd(JSON.parse(line));
  }
});

let currentTurn = null;
let turnCounter = 0;
let thinking = 'medium';
const uiCancelled = [];
const cmdDelayMs = Number(process.env.FAKE_PI_DELAY_CMD_MS || 0);

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + '\\n');
}

function handleCmd(cmd) {
  if (cmd.type === 'extension_ui_response') {
    if (cmd.cancelled && cmd.id) uiCancelled.push(cmd.id);
    return;
  }
  if (cmd.type === 'set_thinking_level') {
    const finish = () => {
      thinking = cmd.level;
      send({ id: cmd.id, type: 'response', command: 'set_thinking_level', success: true });
    };
    if (cmdDelayMs > 0) setTimeout(finish, cmdDelayMs);
    else finish();
    return;
  }
  if (cmd.type === 'new_session') {
    const finish = () => {
      turnCounter = 0;
      send({ id: cmd.id, type: 'response', command: 'new_session', success: true, data: { cancelled: false } });
    };
    if (cmdDelayMs > 0) setTimeout(finish, cmdDelayMs);
    else finish();
    return;
  }
  if (cmd.type === 'clear_queue') {
    send({ id: cmd.id, type: 'response', command: 'clear_queue', success: true, data: { steering: [], followUp: [] } });
    return;
  }
  if (cmd.type === 'steer') {
    if (currentTurn) currentTurn.steered.push(cmd.message);
    send({ id: cmd.id, type: 'response', command: 'steer', success: true, data: { disposition: 'queued' } });
    return;
  }
  if (cmd.type === 'abort') {
    if (currentTurn && !currentTurn.done) {
      currentTurn.done = true;
      clearTimeout(currentTurn.timer);
      send({ id: cmd.id, type: 'response', command: 'abort', success: true });
      send({ type: 'agent_settled', aborted: true });
    } else {
      send({ id: cmd.id, type: 'response', command: 'abort', success: true });
    }
    return;
  }
  if (cmd.type === 'prompt') {
    const msg = cmd.message || '';
    if (msg.includes('FAIL_PROMPT_CMD')) {
      send({ id: cmd.id, type: 'response', command: 'prompt', success: false, error: 'Prompt rejected by stub' });
      return;
    }
    if (msg.includes('CRASH_NOW')) {
      send({ id: cmd.id, type: 'response', command: 'prompt', success: true, data: { disposition: 'started' } });
      setTimeout(() => process.exit(42), 20);
      return;
    }
    if (msg.includes('SPAWN_SUBCHILD')) {
      bgChild = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
      if (process.env.FAKE_PI_SUBCHILD_FILE) {
        writeFileSync(process.env.FAKE_PI_SUBCHILD_FILE, String(bgChild.pid));
      }
    }

    turnCounter += 1;
    send({ id: cmd.id, type: 'response', command: 'prompt', success: true, data: { disposition: 'started' } });

    const delayMs = msg.includes('SLOW:') ? 350 : 25;
    currentTurn = { done: false, steered: [], timer: null };

    if (msg.includes('EXT_UI')) {
      send({ type: 'extension_ui_request', id: 'ui-1', method: 'confirm', title: 'Proceed?' });
    }

    currentTurn.timer = setTimeout(() => {
      if (currentTurn.done) return;
      currentTurn.done = true;

      if (msg.includes('MODIFY_FILE:')) {
        const target = msg.split('MODIFY_FILE:')[1].trim().split(/\\s+/)[0];
        writeFileSync(path.join(process.cwd(), target), 'modified-by-stub\\n');
      }

      if (msg.includes('ASSISTANT_ERROR_ONLY')) {
        send({
          type: 'message_end',
          message: {
            role: 'assistant',
            content: [{ type: 'text', text: '' }],
            stopReason: 'error',
            errorMessage: 'rate limit exceeded',
          },
        });
        send({ type: 'agent_settled', aborted: false });
        return;
      }

      if (msg.includes('ASSISTANT_ERROR_THEN_RETRY')) {
        send({
          type: 'message_end',
          message: {
            role: 'assistant',
            content: [{ type: 'text', text: '' }],
            stopReason: 'error',
            errorMessage: 'transient error',
          },
        });
        send({
          type: 'message_end',
          message: {
            role: 'assistant',
            content: [{ type: 'text', text: 'recovered after retry' }],
            stopReason: 'stop',
          },
        });
        send({ type: 'agent_settled', aborted: false });
        return;
      }

      let replyText = \`pid=\${process.pid} turn=\${turnCounter} thinking=\${thinking}\`;
      if (uiCancelled.length > 0) {
        replyText += \` ui_cancelled=\${uiCancelled.join(',')}\`;
      }
      if (currentTurn.steered.length > 0) {
        replyText += \` steered=\${currentTurn.steered.join(',')}\`;
      }
      if (msg.includes('UNICODE_SEPS')) {
        replyText += ' line1\\u2028line2\\u2029line3';
      }
      if (msg.includes('HUGE_REPLY')) {
        replyText = 'A'.repeat(120 * 1024);
      }

      send({ type: 'tool_execution_start', toolName: 'read' });
      send({
        type: 'message_end',
        message: {
          role: 'assistant',
          content: [{ type: 'text', text: replyText }],
          usage: { input: 10, output: 20, cacheRead: 5, cacheWrite: 0, totalTokens: 35 },
        },
      });
      send({ type: 'agent_settled', aborted: false });
    }, delayMs);
    return;
  }
  send({ id: cmd.id, type: 'response', command: cmd.type, success: false, error: 'Unknown cmd' });
}
`;
  writeFileSync(stubPath, script, 'utf8');
  chmodSync(stubPath, 0o755);
  return stubPath;
}

class McpClient {
  constructor(proc) {
    this.proc = proc;
    this.nextId = 1;
    this.pending = new Map();
    this.stderrChunks = [];
    let buf = '';
    proc.stdout.setEncoding('utf8');
    proc.stdout.on('data', (chunk) => {
      buf += chunk;
      let idx;
      while ((idx = buf.indexOf('\n')) !== -1) {
        const line = buf.slice(0, idx);
        buf = buf.slice(idx + 1);
        if (!line.trim()) continue;
        const frame = JSON.parse(line);
        if (frame.id !== undefined && frame.id !== null && this.pending.has(frame.id)) {
          const { resolve } = this.pending.get(frame.id);
          this.pending.delete(frame.id);
          resolve(frame);
        } else if (frame.id === null && this.pending.has('__null__')) {
          const { resolve } = this.pending.get('__null__');
          this.pending.delete('__null__');
          resolve(frame);
        }
      }
    });
    proc.stderr.setEncoding('utf8');
    proc.stderr.on('data', (c) => this.stderrChunks.push(c));
  }

  request(method, params) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Timeout waiting for ${method} (id=${id})`));
      }, 10000);
      this.pending.set(id, {
        resolve: (frame) => {
          clearTimeout(t);
          resolve(frame);
        },
      });
      this.proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  }

  notify(method, params) {
    this.proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, params }) + '\n');
  }

  sendRaw(rawLine, expectedId = '__null__') {
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        this.pending.delete(expectedId);
        reject(new Error(`Timeout waiting for frame id=${expectedId}`));
      }, 5000);
      this.pending.set(expectedId, {
        resolve: (frame) => {
          clearTimeout(t);
          resolve(frame);
        },
      });
      this.proc.stdin.write(rawLine);
    });
  }

  async callTool(name, args) {
    const frame = await this.request('tools/call', { name, arguments: args });
    assert.ok(frame.result, `Expected result frame, got: ${JSON.stringify(frame)}`);
    const payload = JSON.parse(frame.result.content[0].text);
    return { isError: Boolean(frame.result.isError), payload, rawFrame: frame };
  }

  async close() {
    if (this.proc.exitCode !== null) return;
    this.proc.stdin.end();
    await new Promise((resolve) => {
      const t = setTimeout(() => {
        this.proc.kill('SIGKILL');
        resolve();
      }, 4000);
      this.proc.once('exit', () => {
        clearTimeout(t);
        resolve();
      });
    });
  }
}

function startServer(envOverrides = {}) {
  const proc = spawn(process.execPath, [SERVER_PATH], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, ...envOverrides },
  });
  return new McpClient(proc);
}

test('mcp_config.json conforms to Antigravity plugin spec', () => {
  assert.ok(existsSync(MCP_CONFIG_PATH), 'mcp_config.json must exist');
  const cfg = JSON.parse(readFileSync(MCP_CONFIG_PATH, 'utf8'));
  assert.equal(cfg.variables, undefined, 'variables array must not be present');
  const server = cfg.mcpServers['pi-rpc'];
  assert.equal(server.command, '/bin/bash');
  assert.deepEqual(server.args, ['${PLUGIN_ROOT}/scripts/pi-rpc-mcp.sh']);
  assert.equal(server.timeoutSeconds, 600);
  for (const tool of ['pi_run', 'pi_wait', 'pi_steer', 'pi_abort', 'pi_sessions']) {
    assert.equal(server.tools[tool]?.eager, true, `${tool} must have eager: true`);
  }
});

test('MCP handshake, ping, tools/list, and JSON-RPC error codes (-32700, -32600, -32601, -32602)', async (t) => {
  const dir = makeTempDir();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const piStub = createFakePiStub(dir);
  const client = startServer({ GBSTACK_PI_BIN: piStub });
  t.after(() => client.close());

  const init = await client.request('initialize', { protocolVersion: '2024-11-05' });
  assert.equal(init.result.serverInfo.name, 'pi-rpc-mcp');
  assert.ok(init.result.instructions && init.result.instructions.length > 10);
  client.notify('notifications/initialized');

  const ping = await client.request('ping');
  assert.deepEqual(ping.result, {});

  const list = await client.request('tools/list');
  assert.deepEqual(
    list.result.tools.map((x) => x.name),
    ['pi_run', 'pi_wait', 'pi_steer', 'pi_abort', 'pi_sessions']
  );

  // -32700 Parse Error
  const parseErr = await client.sendRaw('{not valid json\n');
  assert.equal(parseErr.error.code, -32700);

  // -32600 Invalid Request (array payload and missing string method)
  const arrErr = await client.sendRaw('[]\n');
  assert.equal(arrErr.error.code, -32600);
  const noMethodErr = await client.sendRaw('{"jsonrpc":"2.0","id":999}\n', 999);
  assert.equal(noMethodErr.error.code, -32600);

  // -32601 Method Not Found
  const notFound = await client.request('unknown/method');
  assert.equal(notFound.error.code, -32601);

  // -32602 Invalid Params (missing prompt, invalid thinking, invalid wait_ms on pi_run/pi_wait/pi_abort)
  const invalidParams = await client.request('tools/call', { name: 'pi_run', arguments: { cwd: dir } });
  assert.equal(invalidParams.error.code, -32602);
  const badThinking = await client.request('tools/call', {
    name: 'pi_run',
    arguments: { cwd: dir, prompt: 'hi', thinking: 'ultra' },
  });
  assert.equal(badThinking.error.code, -32602);
  const badWait = await client.request('tools/call', {
    name: 'pi_wait',
    arguments: { session_id: 'pi-1', wait_ms: -5 },
  });
  assert.equal(badWait.error.code, -32602);
  const badAbortWait = await client.request('tools/call', {
    name: 'pi_abort',
    arguments: { session_id: 'pi-1', wait_ms: -1 },
  });
  assert.equal(badAbortWait.error.code, -32602);
});

test('LF-only framing preserves U+2028/U+2029, verifies EXT_UI response, keeps thinking on reuse, supports reset, and assistantError status', async (t) => {
  const dir = makeTempDir();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const piStub = createFakePiStub(dir);
  const argsLog = path.join(dir, 'args.jsonl');
  const client = startServer({ GBSTACK_PI_BIN: piStub, FAKE_PI_ARGS_LOG: argsLog });
  t.after(() => client.close());

  const r1 = await client.callTool('pi_run', { cwd: dir, prompt: 'hello UNICODE_SEPS EXT_UI' });
  assert.equal(r1.isError, false);
  assert.equal(r1.payload.status, 'done');
  assert.ok(r1.payload.reply.includes('turn=1'));
  assert.ok(r1.payload.reply.includes('ui_cancelled=ui-1'), 'Server must reply extension_ui_response cancelled:true');
  assert.ok(r1.payload.reply.includes('\u2028'));
  assert.ok(r1.payload.reply.includes('\u2029'));

  const sid = r1.payload.session_id;
  const r2 = await client.callTool('pi_run', { cwd: dir, session_id: sid, prompt: 'second', thinking: 'high' });
  assert.equal(r2.isError, false);
  assert.ok(r2.payload.reply.includes('turn=2'));
  assert.ok(r2.payload.reply.includes('thinking=high'));
  const pid1 = r1.payload.reply.match(/pid=(\d+)/)[1];
  const pid2 = r2.payload.reply.match(/pid=(\d+)/)[1];
  assert.equal(pid1, pid2, 'Reused session must keep same child PID');

  // Reusing session while omitting thinking must preserve 'high' (not downgrade to 'medium')
  const r2b = await client.callTool('pi_run', { cwd: dir, session_id: sid, prompt: 'keep thinking' });
  assert.equal(r2b.isError, false);
  assert.ok(r2b.payload.reply.includes('thinking=high'), 'Omitting thinking on reuse must preserve previous level');

  // Verify spawn args had no --model and no --system-prompt
  const spawned = JSON.parse(readFileSync(argsLog, 'utf8').trim().split('\n')[0]);
  assert.ok(!spawned.args.includes('--model'), 'Must not pass --model when omitted');
  assert.ok(!spawned.args.includes('--system-prompt'), 'Must never pass --system-prompt');

  // assistantError: error-only settles with status='error'; error followed by healthy retry clears error
  const errOnly = await client.callTool('pi_run', { cwd: dir, session_id: sid, prompt: 'ASSISTANT_ERROR_ONLY' });
  assert.equal(errOnly.isError, false);
  assert.equal(errOnly.payload.status, 'error');
  assert.equal(errOnly.payload.error, 'rate limit exceeded');

  const errRetry = await client.callTool('pi_run', { cwd: dir, session_id: sid, prompt: 'ASSISTANT_ERROR_THEN_RETRY' });
  assert.equal(errRetry.isError, false);
  assert.equal(errRetry.payload.status, 'done');
  assert.equal(errRetry.payload.error, undefined);
  assert.equal(errRetry.payload.reply, 'recovered after retry');

  // Reset keeps PID but resets turn counter
  const rst = await client.callTool('pi_sessions', { action: 'reset', session_id: sid });
  assert.equal(rst.isError, false);
  assert.equal(rst.payload.status, 'reset');

  const r3 = await client.callTool('pi_run', { cwd: dir, session_id: sid, prompt: 'after reset' });
  assert.ok(r3.payload.reply.includes(`pid=${pid1}`));
  assert.ok(r3.payload.reply.includes('turn=1'));
});

test('Async handoff: short wait_ms, idempotent pi_wait, pi_steer (running ok / idle rejected), and pi_abort', async (t) => {
  const dir = makeTempDir();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const piStub = createFakePiStub(dir);
  const client = startServer({ GBSTACK_PI_BIN: piStub });
  t.after(() => client.close());

  const started = await client.callTool('pi_run', { cwd: dir, prompt: 'SLOW: task1', wait_ms: 30 });
  assert.equal(started.payload.status, 'running');
  const sid = started.payload.session_id;

  const steered = await client.callTool('pi_steer', { session_id: sid, message: 'focus' });
  assert.equal(steered.payload.status, 'steered');

  const waited = await client.callTool('pi_wait', { session_id: sid, wait_ms: 2000 });
  assert.equal(waited.payload.status, 'done');
  assert.ok(waited.payload.reply.includes('steered=focus'));

  // Idle pi_steer must be rejected so it cannot pollute future runs
  const idleSteer = await client.callTool('pi_steer', { session_id: sid, message: 'too late' });
  assert.equal(idleSteer.isError, true);
  assert.match(idleSteer.payload.error, /running session/i);

  // Idempotent pi_wait after completion
  const waitedAgain = await client.callTool('pi_wait', { session_id: sid, wait_ms: 100 });
  assert.equal(waitedAgain.payload.status, 'done');
  assert.equal(waitedAgain.payload.reply, waited.payload.reply);

  // Abort test
  const slow2 = await client.callTool('pi_run', { cwd: dir, session_id: sid, prompt: 'SLOW: task2', wait_ms: 20 });
  assert.equal(slow2.payload.status, 'running');
  const aborted = await client.callTool('pi_abort', { session_id: sid, wait_ms: 2000 });
  assert.equal(aborted.payload.status, 'aborted');
});

test('Workspace lock (concurrent race & first-turn failure release), conflict rejection, and LRU maxSessions capacity race', async (t) => {
  const dir = makeTempDir();
  const dir2 = makeTempDir();
  const dir3 = makeTempDir();
  t.after(() => {
    rmSync(dir, { recursive: true, force: true });
    rmSync(dir2, { recursive: true, force: true });
    rmSync(dir3, { recursive: true, force: true });
  });
  const piStub = createFakePiStub(dir);
  const client = startServer({ GBSTACK_PI_BIN: piStub, GBSTACK_PI_MAX_SESSIONS: '2' });
  t.after(() => client.close());

  // First-turn failure on new session must release write lock and remove session
  const failedNew = await client.callTool('pi_run', { cwd: dir, prompt: 'FAIL_PROMPT_CMD' });
  assert.equal(failedNew.isError, true);
  assert.match(failedNew.payload.error, /Prompt rejected by stub/i);
  const afterFailList = await client.callTool('pi_sessions', { action: 'list' });
  assert.equal(afterFailList.payload.sessions.length, 0);

  // Concurrent writable pi_run on same cwd: exactly one acquires lock, other fails with writable error
  const [raceA, raceB] = await Promise.all([
    client.callTool('pi_run', { cwd: dir, prompt: 'race1' }),
    client.callTool('pi_run', { cwd: dir, prompt: 'race2' }),
  ]);
  const raceSuccesses = [raceA, raceB].filter((r) => !r.isError);
  const raceFailures = [raceA, raceB].filter((r) => r.isError);
  assert.equal(raceSuccesses.length, 1);
  assert.equal(raceFailures.length, 1);
  assert.match(raceFailures[0].payload.error, /writable/i);
  const w1 = raceSuccesses[0];

  // Reuse conflict rejection (readonly mismatch) with explicit error assertion
  const conflict = await client.callTool('pi_run', { cwd: dir, session_id: w1.payload.session_id, readonly: true, prompt: 'x' });
  assert.equal(conflict.isError, true);
  assert.match(conflict.payload.error, /readonly conflict/i);

  // Concurrent readonly session on same dir succeeds (fills maxSessions=2)
  const ro1 = await client.callTool('pi_run', { cwd: dir, readonly: true, prompt: 'ro1' });
  assert.equal(ro1.isError, false);

  // Creating 3rd session on dir2 evicts LRU (w1)
  const w3 = await client.callTool('pi_run', { cwd: dir2, prompt: 'w3' });
  assert.equal(w3.isError, false);

  const list = await client.callTool('pi_sessions', { action: 'list' });
  assert.equal(list.payload.sessions.length, 2);
  assert.ok(!list.payload.sessions.some((s) => s.session_id === w1.payload.session_id));

  // Concurrent capacity race when maxSessions=2 and all slots are running (none idle to evict)
  await client.callTool('pi_sessions', { action: 'close' });
  const capResults = await Promise.all([
    client.callTool('pi_run', { cwd: dir, prompt: 'SLOW: c1', wait_ms: 20 }),
    client.callTool('pi_run', { cwd: dir2, prompt: 'SLOW: c2', wait_ms: 20 }),
    client.callTool('pi_run', { cwd: dir3, prompt: 'SLOW: c3', wait_ms: 20 }),
  ]);
  const capOk = capResults.filter((r) => !r.isError);
  const capErr = capResults.filter((r) => r.isError);
  assert.equal(capOk.length, 2);
  assert.equal(capErr.length, 1);
  assert.match(capErr[0].payload.error, /capacity/i);
});

test('Baseline 4-key validation (gitCleanHead, fileSha1, real & dangling parent symlinks, .. folding), non-Git snapshot, and reply truncation', async (t) => {
  const dir = makeTempDir();
  const nonGitDir = makeTempDir('pi-nongit-');
  const outsideDir = makeTempDir('pi-outside-');
  const stubDir = makeTempDir('pi-stub-');
  t.after(() => {
    rmSync(dir, { recursive: true, force: true });
    rmSync(nonGitDir, { recursive: true, force: true });
    rmSync(outsideDir, { recursive: true, force: true });
    rmSync(stubDir, { recursive: true, force: true });
  });
  const head = initGitRepo(dir);
  const piStub = createFakePiStub(stubDir);
  const client = startServer({ GBSTACK_PI_BIN: piStub });
  t.after(() => client.close());

  // Unknown baseline key rejected with explicit error message
  const badBase = await client.callTool('pi_run', {
    cwd: dir,
    prompt: 'hi',
    baseline: { gitCleanHead: head, extraKey: 1 },
  });
  assert.equal(badBase.isError, true);
  assert.match(badBase.payload.error, /unknown baseline keys: extraKey/i);

  // gitCleanHead mismatch rejected
  const badHead = await client.callTool('pi_run', {
    cwd: dir,
    prompt: 'hi',
    baseline: { gitCleanHead: '0'.repeat(40) },
  });
  assert.equal(badHead.isError, true);
  assert.match(badHead.payload.error, /gitCleanHead mismatch/i);

  // fileSha1 match & mismatch
  const readmeSha1 = createHash('sha1').update(readFileSync(path.join(dir, 'README.md'))).digest('hex');
  const badSha = await client.callTool('pi_run', {
    cwd: dir,
    prompt: 'hi',
    baseline: { fileSha1: { 'README.md': '0'.repeat(40) } },
  });
  assert.equal(badSha.isError, true);
  assert.match(badSha.payload.error, /fileSha1 mismatch/i);

  const okSha = await client.callTool('pi_run', {
    cwd: dir,
    readonly: true,
    prompt: 'hi',
    baseline: { fileSha1: { 'README.md': readmeSha1 } },
  });
  assert.equal(okSha.isError, false);

  // Real parent directory symlink escape rejected
  symlinkSync(outsideDir, path.join(dir, 'escape-dir'));
  const escReal = await client.callTool('pi_run', {
    cwd: dir,
    prompt: 'hi',
    baseline: { plannedNew: ['escape-dir/pwn.txt'] },
  });
  assert.equal(escReal.isError, true);
  assert.match(escReal.payload.error, /parent symlink escapes workdir/i);
  rmSync(path.join(dir, 'escape-dir'));

  // Dangling parent directory symlink escape rejected
  symlinkSync(path.join(outsideDir, 'nonexistent-sub'), path.join(dir, 'dangling-dir'));
  const escDangling = await client.callTool('pi_run', {
    cwd: dir,
    prompt: 'hi',
    baseline: { plannedNew: ['dangling-dir/pwn.txt'] },
  });
  assert.equal(escDangling.isError, true);
  assert.match(escDangling.payload.error, /parent symlink escapes workdir/i);
  rmSync(path.join(dir, 'dangling-dir'));

  // '..' folding bypass rejected in both plannedNew and allowedPaths
  const dotDotPlanned = await client.callTool('pi_run', {
    cwd: dir,
    prompt: 'hi',
    baseline: { plannedNew: ['a/../b.txt'] },
  });
  assert.equal(dotDotPlanned.isError, true);
  assert.match(dotDotPlanned.payload.error, /cannot contain '\.\.'/i);

  const dotDotAllowed = await client.callTool('pi_run', {
    cwd: dir,
    prompt: 'hi',
    baseline: { allowedPaths: ['a/..'] },
  });
  assert.equal(dotDotAllowed.isError, true);
  assert.match(dotDotAllowed.payload.error, /cannot contain '\.\.'/i);

  // Writable run with allowedPaths detects out_of_scope_files and diff_stat
  const run = await client.callTool('pi_run', {
    cwd: dir,
    prompt: 'MODIFY_FILE:README.md',
    baseline: { gitCleanHead: head, allowedPaths: ['src'] },
  });
  assert.equal(run.isError, false);
  assert.deepEqual(run.payload.changed_files, ['README.md']);
  assert.deepEqual(run.payload.out_of_scope_files, ['README.md']);
  assert.equal(run.payload.readonly_violation, false);
  assert.ok(run.payload.diff_stat.includes('README.md'));

  // Dirty git worktree fails gitCleanHead
  const dirtyHead = await client.callTool('pi_run', {
    cwd: dir,
    readonly: true,
    prompt: 'hi',
    baseline: { gitCleanHead: head },
  });
  assert.equal(dirtyHead.isError, true);
  assert.match(dirtyHead.payload.error, /not clean/i);

  // Non-Git directory snapshot detects changed_files & out_of_scope_files
  writeFileSync(path.join(nonGitDir, 'keep.txt'), 'v1\n');
  const nonGitRun = await client.callTool('pi_run', {
    cwd: nonGitDir,
    prompt: 'MODIFY_FILE:extra.txt',
    baseline: { allowedPaths: ['keep.txt'] },
  });
  assert.equal(nonGitRun.isError, false);
  assert.deepEqual(nonGitRun.payload.changed_files, ['extra.txt']);
  assert.deepEqual(nonGitRun.payload.out_of_scope_files, ['extra.txt']);

  // Readonly run that modifies a file triggers readonly_violation
  const roRun = await client.callTool('pi_run', {
    cwd: dir,
    readonly: true,
    prompt: 'MODIFY_FILE:sneaky.txt',
  });
  assert.equal(roRun.payload.readonly_violation, true);
  assert.ok(roRun.payload.changed_files.includes('sneaky.txt'));

  // Huge reply truncated to <= 64 KiB and total frame < 512 KiB
  const huge = await client.callTool('pi_run', { cwd: dir, readonly: true, prompt: 'HUGE_REPLY' });
  assert.ok(Buffer.byteLength(huge.payload.reply, 'utf8') <= 65536);
  assert.ok(Buffer.byteLength(JSON.stringify(huge.rawFrame), 'utf8') < 524288);
});

test('Crash recovery, prompt failure rollback, concurrent reset guard, missing pi error, idle TTL, and EOF + SIGTERM child cleanup', async (t) => {
  const dir = makeTempDir();
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const piStub = createFakePiStub(dir);

  // Missing pi binary error
  const noPiClient = startServer({ GBSTACK_PI_BIN: '/nonexistent/pi-bin-xyz' });
  const noPiRes = await noPiClient.callTool('pi_run', { cwd: dir, prompt: 'hi' });
  assert.equal(noPiRes.isError, true);
  assert.match(noPiRes.payload.error, /GBSTACK_PI_BIN is not an executable file/i);
  await noPiClient.close();

  // Prompt failure rollback keeps existing session idle and usable
  const client = startServer({
    GBSTACK_PI_BIN: piStub,
    GBSTACK_PI_IDLE_TTL_MS: '250',
    FAKE_PI_DELAY_CMD_MS: '60',
  });
  t.after(() => client.close());

  const ok1 = await client.callTool('pi_run', { cwd: dir, prompt: 'init' });
  const sid = ok1.payload.session_id;

  const failPrompt = await client.callTool('pi_run', { cwd: dir, session_id: sid, prompt: 'FAIL_PROMPT_CMD' });
  assert.equal(failPrompt.isError, true);
  assert.match(failPrompt.payload.error, /Prompt rejected by stub/i);

  // Concurrent reset vs pi_run must not corrupt state machine
  const [rstRes, runDuringReset] = await Promise.all([
    client.callTool('pi_sessions', { action: 'reset', session_id: sid }),
    client.callTool('pi_run', { cwd: dir, session_id: sid, prompt: 'during reset' }),
  ]);
  assert.equal(rstRes.isError, false);
  assert.equal(runDuringReset.isError, true);
  assert.match(runDuringReset.payload.error, /busy|cannot start prompt/i);

  const ok2 = await client.callTool('pi_run', { cwd: dir, session_id: sid, prompt: 'still alive' });
  assert.equal(ok2.isError, false);

  // Crash during run
  const crashed = await client.callTool('pi_run', { cwd: dir, session_id: sid, prompt: 'CRASH_NOW' });
  assert.equal(crashed.isError, true);
  assert.match(crashed.payload.error, /exited unexpectedly.*code=42/i);

  // Idle TTL eviction
  const ttlRun = await client.callTool('pi_run', { cwd: dir, prompt: 'ttl' });
  assert.equal(ttlRun.isError, false);
  await new Promise((r) => setTimeout(r, 600));
  const listAfterTtl = await client.callTool('pi_sessions', { action: 'list' });
  assert.equal(listAfterTtl.payload.sessions.length, 0);

  // Process group cleanup on EOF
  const subchildEof = path.join(dir, 'subchild-eof.pid');
  const clientEof = startServer({ GBSTACK_PI_BIN: piStub, FAKE_PI_SUBCHILD_FILE: subchildEof });
  await clientEof.callTool('pi_run', { cwd: dir, prompt: 'SPAWN_SUBCHILD' });
  const subPidEof = Number(readFileSync(subchildEof, 'utf8').trim());
  await clientEof.close();
  await new Promise((r) => setTimeout(r, 150));
  assert.throws(() => process.kill(subPidEof, 0), /ESRCH/);

  // Process group cleanup on real SIGTERM
  const subchildTerm = path.join(dir, 'subchild-term.pid');
  const clientTerm = startServer({ GBSTACK_PI_BIN: piStub, FAKE_PI_SUBCHILD_FILE: subchildTerm });
  await clientTerm.callTool('pi_run', { cwd: dir, prompt: 'SPAWN_SUBCHILD' });
  const subPidTerm = Number(readFileSync(subchildTerm, 'utf8').trim());
  const exitCode = await new Promise((resolve) => {
    clientTerm.proc.once('exit', (code) => resolve(code));
    clientTerm.proc.kill('SIGTERM');
  });
  assert.equal(exitCode, 0);
  await new Promise((r) => setTimeout(r, 150));
  assert.throws(() => process.kill(subPidTerm, 0), /ESRCH/);
});

test('JsonlDecoder handles split multi-byte UTF-8, maxLineBytes recovery, non-swallowed onRecord errors, and truncateUtf8 produces no U+FFFD', () => {
  const records = [];
  const errors = [];
  const dec = new JsonlDecoder({
    maxLineBytes: 40,
    onRecord: (r) => records.push(r),
    onError: (e) => errors.push(e),
  });

  // Multi-byte UTF-8 (Chinese + 4-byte emoji) pushed 1 byte at a time
  const utf8Line = Buffer.from(JSON.stringify({ msg: '你好🚀世界' }) + '\n', 'utf8');
  for (let i = 0; i < utf8Line.length; i += 1) {
    dec.push(utf8Line.subarray(i, i + 1));
  }
  assert.deepEqual(records, [{ msg: '你好🚀世界' }]);
  assert.equal(errors.length, 0);

  // Oversized line triggers onError, then decoder recovers cleanly on next valid line
  dec.push(Buffer.from(JSON.stringify({ x: 'a'.repeat(100) }) + '\n{"ok":true}\n', 'utf8'));
  assert.equal(errors.length, 1);
  assert.match(errors[0].message, /maxLineBytes/);
  assert.deepEqual(records[1], { ok: true });

  // onRecord exceptions must NOT be swallowed as parse errors
  let onErrCalled = false;
  const throwingDec = new JsonlDecoder({
    onRecord: () => {
      throw new Error('callback boom');
    },
    onError: () => {
      onErrCalled = true;
    },
  });
  assert.throws(() => throwingDec.push('{"a":1}\n'), /callback boom/);
  assert.equal(onErrCalled, false);

  // truncateUtf8 never produces U+FFFD across boundary byte budgets
  const multiByte = '中文🚀测试🔥'.repeat(50);
  for (const limit of [1, 2, 3, 4, 5, 17, 50, 63, 64, 65, 100]) {
    const out = truncateUtf8(multiByte, limit);
    assert.ok(Buffer.byteLength(out, 'utf8') <= limit, `Exceeded limit=${limit}`);
    assert.ok(!out.includes('\uFFFD'), `Produced U+FFFD at limit=${limit}`);
  }
});

test('pi-rpc-mcp.sh fail-fasts on invalid GBSTACK_NODE_BIN, handles spaced HOME, and discovers Node >= 18 under minimal PATH', () => {
  assert.ok(existsSync(LAUNCHER_PATH), 'scripts/pi-rpc-mcp.sh must exist');

  // Explicit invalid GBSTACK_NODE_BIN must fail-fast exit 127 even when real PATH has valid node
  const invalidExplicit = spawnSync('/bin/bash', [LAUNCHER_PATH], {
    env: { ...process.env, GBSTACK_NODE_BIN: '/nonexistent/node' },
    input: '',
    encoding: 'utf8',
  });
  assert.equal(invalidExplicit.status, 127);
  assert.equal(invalidExplicit.stdout, '');
  assert.match(invalidExplicit.stderr, /GBSTACK_NODE_BIN/);

  // HOME with spaces must not word-split when discovering node under ~/.nvm
  const spacedBase = makeTempDir('fake home with spaces-');
  const nvmNodeDir = path.join(spacedBase, '.nvm', 'versions', 'node', 'v20.0.0', 'bin');
  mkdirSync(nvmNodeDir, { recursive: true });
  symlinkSync(process.execPath, path.join(nvmNodeDir, 'node'));
  const initReq = JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} }) + '\n';
  const spacedOk = spawnSync('/bin/bash', [LAUNCHER_PATH], {
    env: { PATH: '/usr/bin:/bin', HOME: spacedBase },
    input: initReq,
    encoding: 'utf8',
    timeout: 8000,
  });
  rmSync(spacedBase, { recursive: true, force: true });
  assert.equal(spacedOk.status, 0, `launcher with spaced HOME failed: ${spacedOk.stderr}`);
  const spacedResp = JSON.parse(spacedOk.stdout.trim().split('\n')[0]);
  assert.equal(spacedResp.id, 1);
  assert.equal(spacedResp.result.serverInfo.name, 'pi-rpc-mcp');

  // Minimal PATH=/usr/bin:/bin with current HOME discovers nvm/homebrew node and answers initialize
  const ok = spawnSync('/bin/bash', [LAUNCHER_PATH], {
    env: { PATH: '/usr/bin:/bin', HOME: process.env.HOME || '' },
    input: initReq,
    encoding: 'utf8',
    timeout: 8000,
  });
  assert.equal(ok.status, 0, `launcher failed: ${ok.stderr}`);
  const resp = JSON.parse(ok.stdout.trim().split('\n')[0]);
  assert.equal(resp.id, 1);
  assert.equal(resp.result.serverInfo.name, 'pi-rpc-mcp');
});
