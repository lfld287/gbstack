import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import net from 'node:net';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JsonlDecoder, truncateUtf8 } from '../scripts/rpc/protocol.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..');
const CTL_SH = path.join(REPO_ROOT, 'scripts', 'pi-rpc-ctl.sh');
const DAEMON_JS = path.join(REPO_ROOT, 'scripts', 'rpc', 'pi-rpc-daemon.mjs');

function makeTempDir(prefix = 'pi-rpc-daemon-test-') {
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
import { spawn } from 'node:child_process';
import path from 'node:path';

const args = process.argv.slice(2);
if (process.env.FAKE_PI_ARGS_LOG) {
  appendFileSync(process.env.FAKE_PI_ARGS_LOG, JSON.stringify({ pid: process.pid, args }) + '\\n');
}

const exitDelayMs = Number(process.env.FAKE_PI_EXIT_DELAY_MS || 0);
if (exitDelayMs > 0) {
  process.on('SIGTERM', () => {
    setTimeout(() => process.exit(0), exitDelayMs);
  });
}

let bgChild = null;
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

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + '\\n');
}

function handleCmd(cmd) {
  if (cmd.type === 'extension_ui_response') return;
  if (cmd.type === 'set_thinking_level') {
    thinking = cmd.level;
    send({ id: cmd.id, type: 'response', command: 'set_thinking_level', success: true });
    return;
  }
  if (cmd.type === 'new_session') {
    turnCounter = 0;
    send({ id: cmd.id, type: 'response', command: 'new_session', success: true, data: { cancelled: false } });
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
    if (msg.includes('SPAWN_SUBCHILD')) {
      bgChild = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
      if (process.env.FAKE_PI_SUBCHILD_FILE) {
        writeFileSync(process.env.FAKE_PI_SUBCHILD_FILE, String(bgChild.pid));
      }
    }
    turnCounter += 1;
    send({ id: cmd.id, type: 'response', command: 'prompt', success: true, data: { disposition: 'started' } });

    const delayMs = msg.includes('SLOW') ? 450 : 25;
    currentTurn = { done: false, steered: [], timer: null };

    currentTurn.timer = setTimeout(() => {
      if (currentTurn.done) return;
      currentTurn.done = true;

      if (msg.includes('MODIFY_FILE:')) {
        const target = msg.split('MODIFY_FILE:')[1].trim().split(/\\s+/)[0];
        writeFileSync(path.join(process.cwd(), target), 'modified-by-stub\\n');
      }

      let replyText = \`pid=\${process.pid} turn=\${turnCounter} thinking=\${thinking}\`;
      if (currentTurn.steered.length > 0) {
        replyText += \` steered=\${currentTurn.steered.join(',')}\`;
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

function runCtl(args, { input = '', env = {}, cwd } = {}) {
  return new Promise((resolve) => {
    const proc = spawn(CTL_SH, args, {
      cwd,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, ...env },
    });
    let stdout = '';
    let stderr = '';
    proc.stdout.setEncoding('utf8');
    proc.stderr.setEncoding('utf8');
    proc.stdout.on('data', (c) => {
      stdout += c;
    });
    proc.stderr.on('data', (c) => {
      stderr += c;
    });
    proc.on('close', (code, signal) => {
      resolve({ code, signal, stdout, stderr });
    });
    if (input !== undefined && input !== null) {
      proc.stdin.end(input);
    } else {
      proc.stdin.end();
    }
  });
}

function isPidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function stopDaemonQuiet(env) {
  await runCtl(['daemon', 'stop'], { env });
}

test('old MCP files are removed and pi-rpc-ctl.sh is executable', () => {
  assert.equal(existsSync(path.join(REPO_ROOT, 'mcp_config.json')), false);
  assert.equal(existsSync(path.join(REPO_ROOT, 'scripts', 'pi-rpc-mcp.sh')), false);
  assert.equal(existsSync(path.join(REPO_ROOT, 'scripts', 'mcp')), false);
  assert.equal(existsSync(path.join(REPO_ROOT, 'tests', 'test_pi_rpc_mcp.mjs')), false);
  const st = statSync(CTL_SH);
  assert.ok((st.mode & 0o111) !== 0, 'pi-rpc-ctl.sh must be executable');
});

test('JsonlDecoder preserves U+2028/U+2029 and truncateUtf8 stays within maxBytes', () => {
  const records = [];
  const decoder = new JsonlDecoder({ onRecord: (r) => records.push(r) });
  decoder.push('{"text":"a\u2028b\u2029c"}\n');
  decoder.flush();
  assert.equal(records.length, 1);
  assert.equal(records[0].text, 'a\u2028b\u2029c');

  const truncated = truncateUtf8('😀'.repeat(50), 64);
  assert.ok(Buffer.byteLength(truncated, 'utf8') <= 64);
});

test('concurrent 5-way CLI lazy start spawns only 1 daemon and enforces 0700/0600 permissions', async (t) => {
  const tmp = makeTempDir();
  const sockDir = path.join(tmp, 'sock');
  const sockPath = path.join(sockDir, 'daemon.sock');
  const workdir = path.join(tmp, 'repo');
  mkdirSync(workdir);
  initGitRepo(workdir);
  const fakePi = createFakePiStub(tmp);
  const env = { GBSTACK_PI_RPC_SOCK: sockPath, GBSTACK_PI_BIN: fakePi };

  t.after(async () => {
    await stopDaemonQuiet(env);
    rmSync(tmp, { recursive: true, force: true });
  });

  const results = await Promise.all(
    Array.from({ length: 5 }, (_, i) =>
      runCtl(['run', '--cwd', workdir, '--readonly', '--json'], { input: `hello ${i}`, env })
    )
  );

  for (const r of results) {
    assert.equal(r.code, 0, `run failed: ${r.stderr}`);
    const payload = JSON.parse(r.stdout);
    assert.equal(payload.status, 'done');
  }

  const dirMode = statSync(sockDir).mode & 0o777;
  const sockMode = statSync(sockPath).mode & 0o777;
  const pidPath = path.join(sockDir, 'daemon.pid');
  const pidMode = statSync(pidPath).mode & 0o777;
  assert.equal(dirMode, 0o700);
  assert.equal(sockMode, 0o600);
  assert.equal(pidMode, 0o600);

  const statusRes = await runCtl(['daemon', 'status', '--json'], { env });
  assert.equal(statusRes.code, 0);
  const status = JSON.parse(statusRes.stdout);
  const pidInfo = JSON.parse(readFileSync(pidPath, 'utf8'));
  assert.equal(status.pid, pidInfo.pid);
});

test('zombie socket cleanup recovers from real dead UDS socket and refuses non-socket regular file', async (t) => {
  const tmp = makeTempDir();
  const sockDir = path.join(tmp, 'sock');
  const sockPath = path.join(sockDir, 'daemon.sock');
  mkdirSync(sockDir, { mode: 0o700 });
  const fakePi = createFakePiStub(tmp);
  const env = { GBSTACK_PI_RPC_SOCK: sockPath, GBSTACK_PI_BIN: fakePi };

  t.after(async () => {
    await stopDaemonQuiet(env);
    rmSync(tmp, { recursive: true, force: true });
  });

  // 1. Non-socket regular file at sockPath must be refused and NOT deleted
  writeFileSync(sockPath, 'not-a-socket');
  writeFileSync(path.join(sockDir, 'daemon.pid'), JSON.stringify({ pid: 999999 }), { mode: 0o600 });
  const badRes = await runCtl(['daemon', 'start'], { env });
  assert.notEqual(badRes.code, 0);
  assert.match(badRes.stderr, /Refusing to remove non-socket file/);
  assert.equal(readFileSync(sockPath, 'utf8'), 'not-a-socket');
  rmSync(sockPath);

  // 2. Real leftover UDS socket from a SIGKILLed listener must be cleaned up
  const holder = spawn(
    process.execPath,
    [
      '-e',
      `const net = require('node:net'); const s = net.createServer(); s.listen(${JSON.stringify(sockPath)}, () => console.log('READY'));`,
    ],
    { stdio: ['ignore', 'pipe', 'pipe'] }
  );
  await new Promise((resolve, reject) => {
    holder.stdout.on('data', (c) => {
      if (String(c).includes('READY')) resolve();
    });
    holder.on('error', reject);
  });
  holder.kill('SIGKILL');
  await new Promise((resolve) => holder.on('exit', resolve));

  assert.equal(lstatSync(sockPath).isSocket(), true, 'leftover path must be a real UDS socket');
  writeFileSync(path.join(sockDir, 'daemon.pid'), JSON.stringify({ pid: holder.pid }), { mode: 0o600 });

  const startRes = await runCtl(['daemon', 'start'], { env });
  assert.equal(startRes.code, 0, startRes.stderr);
  const started = JSON.parse(startRes.stdout);
  assert.equal(started.ok, true);
});

test('writable cwd mutex, idle writable auto new_session ID rotation, --session-id reuse, and --new-session', async (t) => {
  const tmp = makeTempDir();
  const sockPath = path.join(tmp, 'sock', 'daemon.sock');
  const workdir = path.join(tmp, 'repo');
  mkdirSync(workdir);
  initGitRepo(workdir);
  const fakePi = createFakePiStub(tmp);
  const env = { GBSTACK_PI_RPC_SOCK: sockPath, GBSTACK_PI_BIN: fakePi };

  t.after(async () => {
    await stopDaemonQuiet(env);
    rmSync(tmp, { recursive: true, force: true });
  });

  // 1. Start a slow writable run with --wait-ms 0
  const r1 = await runCtl(['run', '--cwd', workdir, '--wait-ms', '0', '--json'], { input: 'SLOW:first', env });
  assert.equal(r1.code, 0, r1.stderr);
  const p1 = JSON.parse(r1.stdout);
  assert.equal(p1.status, 'running');
  const sid1 = p1.session_id;

  // 2. Concurrent writable run on same cwd must be rejected
  const rConflict = await runCtl(['run', '--cwd', workdir, '--json'], { input: 'second', env });
  assert.equal(rConflict.code, 1);
  assert.match(rConflict.stderr, /already has an active writable session/);

  // Wait for first run to finish
  await new Promise((r) => setTimeout(r, 550));

  // 3. Reuse sid1 explicitly with thinking=high -> same pid, turn=2, thinking=high
  const rReuse = await runCtl(['run', '--cwd', workdir, '--session-id', sid1, '--thinking', 'high', '--json'], {
    input: 'reuse',
    env,
  });
  assert.equal(rReuse.code, 0, rReuse.stderr);
  const pReuse = JSON.parse(rReuse.stdout);
  assert.equal(pReuse.session_id, sid1);
  assert.match(pReuse.reply, /turn=2 thinking=high/);
  const pid1 = /pid=(\d+)/.exec(pReuse.reply)[1];

  // 4. --session-id + --new-session -> same sid1, same pid, turn=1
  const rNewSess = await runCtl(['run', '--cwd', workdir, '--session-id', sid1, '--new-session', '--json'], {
    input: 'reset-run',
    env,
  });
  assert.equal(rNewSess.code, 0, rNewSess.stderr);
  const pNewSess = JSON.parse(rNewSess.stdout);
  assert.equal(pNewSess.session_id, sid1);
  assert.match(pNewSess.reply, new RegExp(`pid=${pid1} turn=1`));

  // 5. Omit --session-id on idle writable session -> rotates session_id, keeps same pid, resets turn=1 & thinking=medium
  const rAutoRotate = await runCtl(['run', '--cwd', workdir, '--json'], { input: 'fresh-task', env });
  assert.equal(rAutoRotate.code, 0, rAutoRotate.stderr);
  const pAuto = JSON.parse(rAutoRotate.stdout);
  assert.notEqual(pAuto.session_id, sid1);
  assert.match(pAuto.reply, new RegExp(`pid=${pid1} turn=1 thinking=medium`));

  // Old session_id should no longer exist
  const rOldGone = await runCtl(['run', '--cwd', workdir, '--session-id', sid1, '--json'], { input: 'old', env });
  assert.equal(rOldGone.code, 1);
  assert.match(rOldGone.stderr, /Session not found/);

  // 6. Idle writable session with different --model closes old session and spawns new pi PID
  const rDiffModel = await runCtl(['run', '--cwd', workdir, '--model', 'other-model', '--json'], {
    input: 'diff-model',
    env,
  });
  assert.equal(rDiffModel.code, 0, rDiffModel.stderr);
  const pDiffModel = JSON.parse(rDiffModel.stdout);
  const pid2 = /pid=(\d+)/.exec(pDiffModel.reply)[1];
  assert.notEqual(pid2, pid1, 'changing model on idle writable session must respawn pi');
});

test('4-key baseline, readonly violation (exit 4), text output format, and invalid baseline (exit 2)', async (t) => {
  const tmp = makeTempDir();
  const sockPath = path.join(tmp, 'sock', 'daemon.sock');
  const workdir = path.join(tmp, 'repo');
  mkdirSync(workdir);
  const head = initGitRepo(workdir);
  const readmeSha1 = createHash('sha1').update(readFileSync(path.join(workdir, 'README.md'))).digest('hex');
  const fakePi = createFakePiStub(tmp);
  const env = { GBSTACK_PI_RPC_SOCK: sockPath, GBSTACK_PI_BIN: fakePi };

  t.after(async () => {
    await stopDaemonQuiet(env);
    rmSync(tmp, { recursive: true, force: true });
  });

  // Unknown baseline key -> exit 2
  const rBadKey = await runCtl(
    ['run', '--cwd', workdir, '--baseline-json', JSON.stringify({ extraKey: 1 })],
    { input: 'hi', env }
  );
  assert.equal(rBadKey.code, 2);

  // Valid 4-key baseline + text mode
  const baseline = {
    gitCleanHead: head,
    fileSha1: { 'README.md': readmeSha1 },
    plannedNew: ['new_file.txt'],
    allowedPaths: ['.'],
  };
  const rText = await runCtl(
    ['run', '--cwd', workdir, '--baseline-json', JSON.stringify(baseline)],
    { input: 'hello text', env }
  );
  assert.equal(rText.code, 0, rText.stderr);
  assert.match(rText.stdout.trim(), /^pid=\d+ turn=1 thinking=medium$/);
  assert.match(rText.stderr, /^\[pi-rpc\] session_id=pi-[0-9a-f]+ status=done duration_ms=\d+ tools=1/);

  // Readonly violation -> exit 4
  const rReadonly = await runCtl(['run', '--cwd', workdir, '--readonly', '--json'], {
    input: 'MODIFY_FILE:README.md',
    env,
  });
  assert.equal(rReadonly.code, 4);
  const pReadonly = JSON.parse(rReadonly.stdout);
  assert.equal(pReadonly.readonly_violation, true);
  assert.deepEqual(pReadonly.changed_files, ['README.md']);
});

test('CLI SIGTERM and SIGKILL disconnect both trigger daemon abort without late writes', async (t) => {
  const tmp = makeTempDir();
  const sockPath = path.join(tmp, 'sock', 'daemon.sock');
  const workdir = path.join(tmp, 'repo');
  mkdirSync(workdir);
  initGitRepo(workdir);
  const fakePi = createFakePiStub(tmp);
  const env = { GBSTACK_PI_RPC_SOCK: sockPath, GBSTACK_PI_BIN: fakePi };

  t.after(async () => {
    await stopDaemonQuiet(env);
    rmSync(tmp, { recursive: true, force: true });
  });

  // Ensure daemon is started first
  const startRes = await runCtl(['daemon', 'start'], { env });
  assert.equal(startRes.code, 0);

  // Case A: SIGTERM to CLI
  const childA = spawn(CTL_SH, ['run', '--cwd', workdir], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, ...env },
  });
  childA.stdin.end('SLOW:MODIFY_FILE:late-term.txt');
  await new Promise((r) => setTimeout(r, 150));
  childA.kill('SIGTERM');
  const exitA = await new Promise((resolve) => childA.on('close', (code) => resolve(code)));
  assert.equal(exitA, 128 + 15);
  await new Promise((r) => setTimeout(r, 450));
  assert.equal(existsSync(path.join(workdir, 'late-term.txt')), false);

  // Case B: SIGKILL to CLI -> UDS disconnect aborts session
  const childB = spawn(CTL_SH, ['run', '--cwd', workdir], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, ...env },
  });
  childB.stdin.end('SLOW:MODIFY_FILE:late-kill.txt');
  await new Promise((r) => setTimeout(r, 150));
  childB.kill('SIGKILL');
  await new Promise((resolve) => childB.on('close', resolve));
  await new Promise((r) => setTimeout(r, 450));
  assert.equal(existsSync(path.join(workdir, 'late-kill.txt')), false);

  const sessRes = await runCtl(['sessions', 'list', '--json'], { env });
  assert.equal(sessRes.code, 0);
  const sessList = JSON.parse(sessRes.stdout).sessions;
  assert.ok(sessList.every((s) => s.state === 'idle'), 'all sessions must settle back to idle');
});

test('steer, abort, sessions, daemon status/stop, and preflight subcommands', async (t) => {
  const tmp = makeTempDir();
  const sockPath = path.join(tmp, 'sock', 'daemon.sock');
  const workdir = path.join(tmp, 'repo');
  mkdirSync(workdir);
  initGitRepo(workdir);
  const fakePi = createFakePiStub(tmp);
  const env = { GBSTACK_PI_RPC_SOCK: sockPath, GBSTACK_PI_BIN: fakePi };

  t.after(async () => {
    await stopDaemonQuiet(env);
    rmSync(tmp, { recursive: true, force: true });
  });

  // preflight OK & missing pi -> 127
  const pfOk = await runCtl(['preflight'], { env });
  assert.equal(pfOk.code, 0);
  assert.match(pfOk.stdout, /Preflight OK: pi-rpc/);

  const pfMiss = await runCtl(['preflight'], { env: { ...env, GBSTACK_PI_BIN: '/nonexistent/pi' } });
  assert.equal(pfMiss.code, 127);

  // daemon status when not running -> exit 1 without auto-start
  const stDown = await runCtl(['daemon', 'status'], { env });
  assert.equal(stDown.code, 1);

  // Start slow run with --wait-ms 0, check sessions list active_run, then steer, then abort
  const rRun = await runCtl(['run', '--cwd', workdir, '--wait-ms', '0', '--json'], { input: 'SLOW:steer-test', env });
  assert.equal(rRun.code, 0);
  const sid = JSON.parse(rRun.stdout).session_id;

  const listRunning = await runCtl(['sessions', 'list', '--json'], { env });
  assert.equal(listRunning.code, 0);
  const active = JSON.parse(listRunning.stdout).sessions.find((s) => s.session_id === sid);
  assert.ok(active.active_run, 'running session must report active_run');

  const steerRes = await runCtl(['steer', '--session-id', sid, '--message', 'focus'], { env });
  assert.equal(steerRes.code, 0);
  assert.equal(JSON.parse(steerRes.stdout).status, 'steered');

  const abortRes = await runCtl(['abort', '--session-id', sid], { env });
  assert.equal(abortRes.code, 0);
  assert.equal(JSON.parse(abortRes.stdout).status, 'aborted');

  const resetRes = await runCtl(['sessions', 'reset', '--session-id', sid], { env });
  assert.equal(resetRes.code, 0);
  assert.equal(JSON.parse(resetRes.stdout).status, 'reset');

  const closeRes = await runCtl(['sessions', 'close', '--session-id', sid], { env });
  assert.equal(closeRes.code, 0);

  const stopRes = await runCtl(['daemon', 'stop'], { env });
  assert.equal(stopRes.code, 0);
  assert.equal(JSON.parse(stopRes.stdout).stopped, true);
});

test('daemon idle TTL auto-exits and leaves no orphan processes', async (t) => {
  const tmp = makeTempDir();
  const sockDir = path.join(tmp, 'sock');
  const sockPath = path.join(sockDir, 'daemon.sock');
  const workdir = path.join(tmp, 'repo');
  const subchildFile = path.join(tmp, 'subchild.pid');
  mkdirSync(workdir);
  initGitRepo(workdir);
  const fakePi = createFakePiStub(tmp);
  const env = {
    GBSTACK_PI_RPC_SOCK: sockPath,
    GBSTACK_PI_BIN: fakePi,
    GBSTACK_PI_IDLE_TTL_MS: '120',
    GBSTACK_PI_DAEMON_IDLE_TTL_MS: '200',
    FAKE_PI_SUBCHILD_FILE: subchildFile,
  };

  t.after(async () => {
    await stopDaemonQuiet(env);
    rmSync(tmp, { recursive: true, force: true });
  });

  const res = await runCtl(['run', '--cwd', workdir, '--json'], { input: 'SPAWN_SUBCHILD', env });
  assert.equal(res.code, 0, res.stderr);
  const pidInfo = JSON.parse(readFileSync(path.join(sockDir, 'daemon.pid'), 'utf8'));
  const subchildPid = Number(readFileSync(subchildFile, 'utf8').trim());

  // Wait for session idle TTL + daemon idle TTL
  const deadline = Date.now() + 4000;
  while (Date.now() < deadline && existsSync(sockPath)) {
    await new Promise((r) => setTimeout(r, 50));
  }

  assert.equal(existsSync(sockPath), false, 'daemon.sock should be cleaned up on idle exit');
  assert.equal(existsSync(path.join(sockDir, 'daemon.pid')), false, 'daemon.pid should be cleaned up');
  assert.equal(isPidAlive(pidInfo.pid), false, 'daemon process must exit');
  assert.equal(isPidAlive(subchildPid), false, 'subchild process must be killed with process group');
});

test('daemon-side baseline & cwd validation failures return exit code 2', async (t) => {
  const tmp = makeTempDir();
  const sockPath = path.join(tmp, 'sock', 'daemon.sock');
  const workdir = path.join(tmp, 'repo');
  mkdirSync(workdir);
  initGitRepo(workdir);
  const fakePi = createFakePiStub(tmp);
  const env = { GBSTACK_PI_RPC_SOCK: sockPath, GBSTACK_PI_BIN: fakePi };

  t.after(async () => {
    await stopDaemonQuiet(env);
    rmSync(tmp, { recursive: true, force: true });
  });

  // gitCleanHead mismatch -> exit 2
  const rHead = await runCtl(
    ['run', '--cwd', workdir, '--baseline-json', JSON.stringify({ gitCleanHead: '0'.repeat(40) })],
    { input: 'check head', env }
  );
  assert.equal(rHead.code, 2, `expected exit 2, got ${rHead.code}: ${rHead.stderr}`);
  assert.match(rHead.stderr, /gitCleanHead mismatch/);

  // fileSha1 mismatch -> exit 2
  const rSha = await runCtl(
    ['run', '--cwd', workdir, '--baseline-json', JSON.stringify({ fileSha1: { 'README.md': '0'.repeat(40) } })],
    { input: 'check sha1', env }
  );
  assert.equal(rSha.code, 2, `expected exit 2, got ${rSha.code}: ${rSha.stderr}`);
  assert.match(rSha.stderr, /fileSha1 mismatch/);

  // plannedNew already exists -> exit 2
  const rPlanned = await runCtl(
    ['run', '--cwd', workdir, '--baseline-json', JSON.stringify({ plannedNew: ['README.md'] })],
    { input: 'check plannedNew', env }
  );
  assert.equal(rPlanned.code, 2, `expected exit 2, got ${rPlanned.code}: ${rPlanned.stderr}`);
  assert.match(rPlanned.stderr, /plannedNew already exists/);

  // invalid --cwd -> exit 2
  const rBadCwd = await runCtl(['run', '--cwd', path.join(tmp, 'does-not-exist')], { input: 'check cwd', env });
  assert.equal(rBadCwd.code, 2, `expected exit 2, got ${rBadCwd.code}: ${rBadCwd.stderr}`);
  assert.match(rBadCwd.stderr, /Invalid cwd/);
});

test('pool-full eviction preserves writable cwd mutex during pending spawn', async (t) => {
  const tmp = makeTempDir();
  const sockPath = path.join(tmp, 'sock', 'daemon.sock');
  const repoA = path.join(tmp, 'repoA');
  const repoB = path.join(tmp, 'repoB');
  mkdirSync(repoA);
  mkdirSync(repoB);
  initGitRepo(repoA);
  initGitRepo(repoB);
  const fakePi = createFakePiStub(tmp);
  const env = {
    GBSTACK_PI_RPC_SOCK: sockPath,
    GBSTACK_PI_BIN: fakePi,
    GBSTACK_PI_MAX_SESSIONS: '1',
    FAKE_PI_EXIT_DELAY_MS: '250',
  };

  t.after(async () => {
    await stopDaemonQuiet(env);
    rmSync(tmp, { recursive: true, force: true });
  });

  // Fill the single pool slot with an idle session in repoA
  const rA = await runCtl(['run', '--cwd', repoA, '--json'], { input: 'fill-slot', env });
  assert.equal(rA.code, 0, rA.stderr);

  // Fire two concurrent writable runs on repoB while evicting repoA takes ~250ms
  const [rB1, rB2] = await Promise.all([
    runCtl(['run', '--cwd', repoB, '--json'], { input: 'concurrent-b-1', env }),
    runCtl(['run', '--cwd', repoB, '--json'], { input: 'concurrent-b-2', env }),
  ]);

  const codes = [rB1.code, rB2.code].sort();
  assert.deepEqual(codes, [0, 1], `expected one success and one mutex rejection, got: ${JSON.stringify([rB1, rB2])}`);
  const rejected = rB1.code === 1 ? rB1 : rB2;
  assert.match(rejected.stderr, /already has an active writable session/);
});

test('relative --cwd resolves against client working directory across different directories', async (t) => {
  const tmp = makeTempDir();
  const sockPath = path.join(tmp, 'sock', 'daemon.sock');
  const parentDir = path.join(tmp, 'parent');
  const subRepo = path.join(parentDir, 'subrepo');
  mkdirSync(subRepo, { recursive: true });
  initGitRepo(subRepo);
  const fakePi = createFakePiStub(tmp);
  const env = { GBSTACK_PI_RPC_SOCK: sockPath, GBSTACK_PI_BIN: fakePi };

  t.after(async () => {
    await stopDaemonQuiet(env);
    rmSync(tmp, { recursive: true, force: true });
  });

  // Start daemon from REPO_ROOT (different cwd from parentDir)
  const startRes = await runCtl(['daemon', 'start'], { env, cwd: REPO_ROOT });
  assert.equal(startRes.code, 0, startRes.stderr);

  // Run CLI from parentDir with relative --cwd ./subrepo
  const runRes = await runCtl(['run', '--cwd', './subrepo', '--json'], {
    input: 'MODIFY_FILE:rel-check.txt',
    env,
    cwd: parentDir,
  });
  assert.equal(runRes.code, 0, runRes.stderr);
  assert.equal(existsSync(path.join(subRepo, 'rel-check.txt')), true);
});

test('sessions close without ID preserves idleSweep TTL eviction for subsequent sessions', async (t) => {
  const tmp = makeTempDir();
  const sockPath = path.join(tmp, 'sock', 'daemon.sock');
  const workdir = path.join(tmp, 'repo');
  mkdirSync(workdir);
  initGitRepo(workdir);
  const fakePi = createFakePiStub(tmp);
  const env = {
    GBSTACK_PI_RPC_SOCK: sockPath,
    GBSTACK_PI_BIN: fakePi,
    GBSTACK_PI_IDLE_TTL_MS: '150',
    GBSTACK_PI_DAEMON_IDLE_TTL_MS: '10000',
  };

  t.after(async () => {
    await stopDaemonQuiet(env);
    rmSync(tmp, { recursive: true, force: true });
  });

  const r1 = await runCtl(['run', '--cwd', workdir, '--json'], { input: 'first', env });
  assert.equal(r1.code, 0, r1.stderr);

  // Close all sessions via `sessions close` (no --session-id)
  const closeAllRes = await runCtl(['sessions', 'close'], { env });
  assert.equal(closeAllRes.code, 0, closeAllRes.stderr);

  // Spawn a new session and verify idleSweep still evicts it after TTL
  const r2 = await runCtl(['run', '--cwd', workdir, '--json'], { input: 'second', env });
  assert.equal(r2.code, 0, r2.stderr);

  const deadline = Date.now() + 2000;
  let remaining = 1;
  while (Date.now() < deadline && remaining > 0) {
    await new Promise((r) => setTimeout(r, 50));
    const listRes = await runCtl(['sessions', 'list', '--json'], { env });
    assert.equal(listRes.code, 0, listRes.stderr);
    remaining = JSON.parse(listRes.stdout).sessions.length;
  }
  assert.equal(remaining, 0, 'idleSweep must continue evicting sessions after `sessions close`');
});
