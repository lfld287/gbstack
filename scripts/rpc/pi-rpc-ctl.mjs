import { execFileSync, spawn } from 'node:child_process';
import {
  closeSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  rmSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import net from 'node:net';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { encodeJsonRpcFrame, JsonlDecoder } from './protocol.mjs';
import { ensureSecureSockDir, resolveSocketPath } from './pi-rpc-daemon.mjs';
import { resolvePiBinary } from './session-pool.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DAEMON_PATH = path.join(__dirname, 'pi-rpc-daemon.mjs');

const THINKING_LEVELS = new Set(['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']);
const ALLOWED_BASELINE_KEYS = new Set(['gitCleanHead', 'fileSha1', 'plannedNew', 'allowedPaths']);

const LOCK_WAIT_MS = 5000;
const LOCK_STALE_MS = 10000;
const POLL_INTERVAL_MS = 50;

class UsageError extends Error {}

function isPidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code === 'EPERM';
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function readStdinUtf8() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on('data', (chunk) => chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
    process.stdin.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    process.stdin.on('error', reject);
  });
}

function callDaemon(sockPath, method, params, { onNotification, onSocket, timeoutMs = null } = {}) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(sockPath);
    onSocket?.(socket);

    let settled = false;
    let timer = null;
    const reqId = 1;

    const cleanup = () => {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    };

    const fail = (err) => {
      if (settled) return;
      settled = true;
      cleanup();
      socket.destroy();
      reject(err);
    };

    if (timeoutMs !== null && timeoutMs > 0) {
      timer = setTimeout(() => {
        fail(new Error(`RPC ${method} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
    }

    const decoder = new JsonlDecoder({
      onRecord: (msg) => {
        if (msg && msg.method && (msg.id === undefined || msg.id === null)) {
          onNotification?.(msg);
          return;
        }
        if (msg && msg.id === null && msg.error) {
          const err = new Error(msg.error.message || 'Daemon RPC error');
          err.rpcCode = msg.error.code;
          err.data = msg.error.data;
          fail(err);
          return;
        }
        if (msg && msg.id === reqId) {
          if (settled) return;
          settled = true;
          cleanup();
          socket.end();
          if (msg.error) {
            const err = new Error(msg.error.message || 'Daemon RPC error');
            err.rpcCode = msg.error.code;
            err.data = msg.error.data;
            reject(err);
          } else {
            resolve(msg.result);
          }
        }
      },
      onError: (err) => {
        fail(new Error(`Malformed JSONL from daemon: ${err.message}`));
      },
    });

    socket.on('connect', () => {
      socket.write(encodeJsonRpcFrame({ jsonrpc: '2.0', id: reqId, method, params }));
    });
    socket.on('data', (chunk) => decoder.push(chunk));
    socket.on('end', () => {
      decoder.flush();
      if (!settled) {
        fail(new Error('Daemon closed connection before sending response'));
      }
    });
    socket.on('error', (err) => fail(err));
  });
}

async function tryPing(sockPath, timeoutMs = 1000) {
  try {
    return await callDaemon(sockPath, 'ping', {}, { timeoutMs });
  } catch {
    return null;
  }
}

function tryCleanupStaleLock(lockDir) {
  try {
    const st = statSync(lockDir);
    if (Date.now() - st.mtimeMs < LOCK_STALE_MS) return false;
    const pidFile = path.join(lockDir, 'pid');
    let pid = null;
    try {
      pid = Number(readFileSync(pidFile, 'utf8').trim());
    } catch {
      pid = null;
    }
    if (!isPidAlive(pid)) {
      rmSync(lockDir, { recursive: true, force: true });
      return true;
    }
  } catch {
    // ignore
  }
  return false;
}

function tryCleanupZombieSocket(sockPath, pidPath) {
  let st;
  try {
    st = lstatSync(sockPath);
  } catch (err) {
    if (err.code === 'ENOENT') return;
    throw err;
  }
  if (!st.isSocket()) {
    throw new Error(`Refusing to remove non-socket file at ${sockPath}`);
  }
  let pid = null;
  try {
    const info = JSON.parse(readFileSync(pidPath, 'utf8'));
    pid = Number(info?.pid);
  } catch {
    pid = null;
  }
  if (!isPidAlive(pid)) {
    try {
      unlinkSync(sockPath);
    } catch {
      // ignore
    }
    try {
      unlinkSync(pidPath);
    } catch {
      // ignore
    }
  }
}

async function ensureDaemonRunning(sockPath) {
  const fastPing = await tryPing(sockPath, 1000);
  if (fastPing) return fastPing;

  const sockDir = path.dirname(sockPath);
  const pidPath = path.join(sockDir, 'daemon.pid');
  const lockDir = path.join(sockDir, 'daemon.lock.d');
  ensureSecureSockDir(sockDir);

  const deadline = Date.now() + LOCK_WAIT_MS;
  let acquired = false;
  while (!acquired) {
    try {
      mkdirSync(lockDir, { mode: 0o700 });
      acquired = true;
    } catch (err) {
      if (err.code !== 'EEXIST') throw err;
      if (tryCleanupStaleLock(lockDir)) continue;
      if (Date.now() >= deadline) {
        throw new Error(`Timed out waiting for daemon startup lock: ${lockDir}`);
      }
      await sleep(POLL_INTERVAL_MS);
    }
  }

  try {
    try {
      writeFileSync(path.join(lockDir, 'pid'), `${process.pid}\n`, { mode: 0o600 });
    } catch {
      // ignore
    }

    const doubleCheck = await tryPing(sockPath, 1000);
    if (doubleCheck) return doubleCheck;

    tryCleanupZombieSocket(sockPath, pidPath);

    const logPath = process.env.GBSTACK_PI_DAEMON_LOG || process.env.GBSTACK_PI_MCP_LOG || path.join(sockDir, 'daemon.log');
    const logFd = openSync(logPath, 'a', 0o600);
    try {
      const child = spawn(process.execPath, [DAEMON_PATH, '--socket', sockPath], {
        detached: true,
        stdio: ['ignore', logFd, logFd],
        env: process.env,
      });
      child.unref();
    } finally {
      closeSync(logFd);
    }

    const readyDeadline = Date.now() + LOCK_WAIT_MS;
    while (Date.now() < readyDeadline) {
      const pong = await tryPing(sockPath, 500);
      if (pong) return pong;
      await sleep(POLL_INTERVAL_MS);
    }
    throw new Error(`Daemon failed to become ready at ${sockPath} within ${LOCK_WAIT_MS}ms`);
  } finally {
    rmSync(lockDir, { recursive: true, force: true });
  }
}

function takeFlagValue(argv, index, flag) {
  if (index + 1 >= argv.length) {
    throw new UsageError(`Missing value for ${flag}`);
  }
  return argv[index + 1];
}

function parseBaselineJson(raw) {
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    throw new UsageError(`Invalid --baseline-json: ${err.message}`);
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new UsageError('--baseline-json must be a JSON object');
  }
  const unknown = Object.keys(parsed).filter((k) => !ALLOWED_BASELINE_KEYS.has(k));
  if (unknown.length > 0) {
    throw new UsageError(`Unknown --baseline-json keys: ${unknown.sort().join(', ')}`);
  }
  return parsed;
}

function parseRunArgs(argv) {
  const opts = {
    cwd: null,
    model: undefined,
    thinking: undefined,
    readonly: false,
    session_id: undefined,
    new_session: false,
    wait_ms: null,
    hasWaitMs: false,
    baseline: undefined,
    json: false,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--cwd') {
      opts.cwd = takeFlagValue(argv, i, arg);
      i += 1;
    } else if (arg === '--model') {
      opts.model = takeFlagValue(argv, i, arg);
      i += 1;
    } else if (arg === '--thinking') {
      const val = takeFlagValue(argv, i, arg);
      if (!THINKING_LEVELS.has(val)) {
        throw new UsageError(`Invalid --thinking ${val}; expected one of: ${[...THINKING_LEVELS].join(', ')}`);
      }
      opts.thinking = val;
      i += 1;
    } else if (arg === '--readonly') {
      opts.readonly = true;
    } else if (arg === '--session-id') {
      opts.session_id = takeFlagValue(argv, i, arg);
      if (!opts.session_id) throw new UsageError('--session-id must be non-empty');
      i += 1;
    } else if (arg === '--new-session') {
      opts.new_session = true;
    } else if (arg === '--wait-ms') {
      const raw = takeFlagValue(argv, i, arg);
      const n = Number(raw);
      if (!Number.isFinite(n) || n < 0) {
        throw new UsageError(`Invalid --wait-ms: ${raw}`);
      }
      opts.wait_ms = n;
      opts.hasWaitMs = true;
      i += 1;
    } else if (arg === '--baseline-json') {
      opts.baseline = parseBaselineJson(takeFlagValue(argv, i, arg));
      i += 1;
    } else if (arg === '--json') {
      opts.json = true;
    } else {
      throw new UsageError(`Unknown option for run: ${arg}`);
    }
  }

  if (!opts.cwd) {
    throw new UsageError('run requires --cwd <DIR>');
  }
  return opts;
}

function normalizeClientCwd(cwd) {
  if (cwd === '~') return homedir();
  if (cwd.startsWith('~/')) return path.resolve(homedir(), cwd.slice(2));
  return path.resolve(cwd);
}

async function cmdRun(argv, sockPath) {
  const opts = parseRunArgs(argv);
  const resolvedCwd = normalizeClientCwd(opts.cwd);
  const prompt = await readStdinUtf8();
  if (!prompt || !prompt.trim()) {
    throw new UsageError('run requires a non-empty prompt on stdin');
  }

  await ensureDaemonRunning(sockPath);

  let activeSessionId = opts.session_id || null;
  let activeSocket = null;
  let signalHandled = false;

  const onSignal = (sigName, signum) => {
    if (signalHandled) return;
    signalHandled = true;
    const finish = () => process.exit(128 + signum);

    const sid = activeSessionId;
    if (sid) {
      const abortPromise = callDaemon(sockPath, 'abort', { session_id: sid, wait_ms: 3000 }, { timeoutMs: 3000 }).catch(
        () => {}
      );
      const hardTimer = setTimeout(() => {
        if (activeSocket) activeSocket.destroy();
        finish();
      }, 3000);
      abortPromise.finally(() => {
        clearTimeout(hardTimer);
        if (activeSocket) activeSocket.destroy();
        finish();
      });
    } else {
      if (activeSocket) activeSocket.destroy();
      finish();
    }
  };

  const onSigTerm = () => onSignal('SIGTERM', 15);
  const onSigInt = () => onSignal('SIGINT', 2);
  process.on('SIGTERM', onSigTerm);
  process.on('SIGINT', onSigInt);

  try {
    let result;
    try {
      result = await callDaemon(
        sockPath,
        'run',
        {
          cwd: resolvedCwd,
          prompt,
          session_id: opts.session_id,
          new_session: opts.new_session,
          thinking: opts.thinking,
          readonly: opts.readonly,
          model: opts.model,
          wait_ms: opts.wait_ms,
          baseline: opts.baseline,
          abort_on_disconnect: !opts.hasWaitMs,
        },
        {
          onSocket: (sock) => {
            activeSocket = sock;
          },
          onNotification: (msg) => {
            if (msg.method === 'run_started' && msg.params?.session_id) {
              activeSessionId = msg.params.session_id;
            }
          },
        }
      );
    } catch (err) {
      if (signalHandled) {
        return await new Promise(() => {});
      }
      throw err;
    }

    if (signalHandled) {
      return await new Promise(() => {});
    }

    if (opts.json) {
      process.stdout.write(JSON.stringify(result) + '\n');
    } else {
      if (result.reply) {
        process.stdout.write(result.reply.endsWith('\n') ? result.reply : result.reply + '\n');
      }
      const durationMs = result.duration_ms ?? result.elapsed_ms ?? 0;
      const toolsCount = Array.isArray(result.tools_used) ? result.tools_used.length : 0;
      process.stderr.write(
        `[pi-rpc] session_id=${result.session_id} status=${result.status} duration_ms=${durationMs} tools=${toolsCount}\n`
      );
      if (result.error) {
        process.stderr.write(`Error: ${result.error}\n`);
      }
    }

    if (result.readonly_violation) return 4;
    if (result.status === 'done' || result.status === 'running') return 0;
    return 1;
  } finally {
    process.off('SIGTERM', onSigTerm);
    process.off('SIGINT', onSigInt);
  }
}

async function cmdSteer(argv, sockPath) {
  let sessionId = null;
  let message = null;
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--session-id') {
      sessionId = takeFlagValue(argv, i, argv[i]);
      i += 1;
    } else if (argv[i] === '--message') {
      message = takeFlagValue(argv, i, argv[i]);
      i += 1;
    } else {
      throw new UsageError(`Unknown option for steer: ${argv[i]}`);
    }
  }
  if (!sessionId) throw new UsageError('steer requires --session-id <ID>');
  if (message === null) {
    message = await readStdinUtf8();
  }
  if (!message || !message.trim()) {
    throw new UsageError('steer requires --message <TEXT> or non-empty stdin');
  }
  const res = await callDaemon(sockPath, 'steer', { session_id: sessionId, message });
  process.stdout.write(JSON.stringify(res) + '\n');
  return 0;
}

async function cmdAbort(argv, sockPath) {
  let sessionId = null;
  let waitMs = undefined;
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--session-id') {
      sessionId = takeFlagValue(argv, i, argv[i]);
      i += 1;
    } else if (argv[i] === '--wait-ms') {
      const raw = takeFlagValue(argv, i, argv[i]);
      const n = Number(raw);
      if (!Number.isFinite(n) || n < 0) throw new UsageError(`Invalid --wait-ms: ${raw}`);
      waitMs = n;
      i += 1;
    } else {
      throw new UsageError(`Unknown option for abort: ${argv[i]}`);
    }
  }
  if (!sessionId) throw new UsageError('abort requires --session-id <ID>');
  const res = await callDaemon(sockPath, 'abort', { session_id: sessionId, wait_ms: waitMs });
  process.stdout.write(JSON.stringify(res) + '\n');
  return 0;
}

async function cmdSessions(argv, sockPath) {
  const action = argv[0];
  if (!action || !['list', 'reset', 'close'].includes(action)) {
    throw new UsageError('sessions requires subcommand: list | reset | close');
  }
  let sessionId = undefined;
  let json = false;
  for (let i = 1; i < argv.length; i += 1) {
    if (argv[i] === '--session-id') {
      sessionId = takeFlagValue(argv, i, argv[i]);
      i += 1;
    } else if (argv[i] === '--json') {
      json = true;
    } else {
      throw new UsageError(`Unknown option for sessions ${action}: ${argv[i]}`);
    }
  }
  if (action === 'reset' && !sessionId) {
    throw new UsageError('sessions reset requires --session-id <ID>');
  }
  const res = await callDaemon(sockPath, 'sessions', { action, session_id: sessionId });
  if (json || action !== 'list') {
    process.stdout.write(JSON.stringify(res) + '\n');
  } else {
    for (const s of res.sessions || []) {
      process.stdout.write(
        `${s.session_id}\tstate=${s.state}\tpid=${s.pid ?? '-'}\treadonly=${s.readonly}\tcwd=${s.cwd}\n`
      );
    }
  }
  return 0;
}

async function cmdDaemon(argv, sockPath) {
  const action = argv[0];
  if (!action || !['status', 'stop', 'start'].includes(action)) {
    throw new UsageError('daemon requires subcommand: status | stop | start');
  }
  let json = false;
  for (let i = 1; i < argv.length; i += 1) {
    if (argv[i] === '--json') {
      json = true;
    } else {
      throw new UsageError(`Unknown option for daemon ${action}: ${argv[i]}`);
    }
  }

  if (action === 'start') {
    const pong = await ensureDaemonRunning(sockPath);
    process.stdout.write(JSON.stringify({ started: true, ...pong }) + '\n');
    return 0;
  }

  if (action === 'status') {
    const res = await callDaemon(sockPath, 'status', {});
    if (json) {
      process.stdout.write(JSON.stringify(res) + '\n');
    } else {
      process.stdout.write(
        `pid=${res.pid} uptime_ms=${res.uptime_ms} sessions=${res.sessions_count}/${res.max_sessions} socket=${res.socket}\n`
      );
    }
    return 0;
  }

  const res = await callDaemon(sockPath, 'stop', {});
  process.stdout.write(JSON.stringify(res) + '\n');
  return 0;
}

async function cmdPreflight(argv, sockPath) {
  let checkAuth = false;
  let model = null;
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--check-auth') {
      checkAuth = true;
    } else if (argv[i] === '--model') {
      model = takeFlagValue(argv, i, argv[i]);
      i += 1;
    } else {
      throw new UsageError(`Unknown option for preflight: ${argv[i]}`);
    }
  }

  const major = parseInt(process.versions.node.split('.')[0], 10);
  if (!Number.isFinite(major) || major < 18) {
    process.stderr.write(`Error: Node.js >= 18 required, found ${process.versions.node}\n`);
    return 127;
  }

  const piBin = resolvePiBinary();
  ensureSecureSockDir(path.dirname(sockPath));

  if (checkAuth) {
    if (!model) {
      throw new UsageError('preflight --check-auth requires --model <M>');
    }
    execFileSync(piBin, ['auth', 'check', '--model', model], { stdio: 'inherit' });
  }

  process.stdout.write(`Preflight OK: pi-rpc (${piBin})\n`);
  return 0;
}

async function main() {
  const argv = process.argv.slice(2);
  const cmd = argv[0];
  if (!cmd) {
    throw new UsageError('Usage: pi-rpc-ctl <run|steer|abort|sessions|daemon|preflight> [options]');
  }

  const sockPath = resolveSocketPath();
  let exitCode = 0;
  if (cmd === 'run') {
    exitCode = await cmdRun(argv.slice(1), sockPath);
  } else if (cmd === 'steer') {
    exitCode = await cmdSteer(argv.slice(1), sockPath);
  } else if (cmd === 'abort') {
    exitCode = await cmdAbort(argv.slice(1), sockPath);
  } else if (cmd === 'sessions') {
    exitCode = await cmdSessions(argv.slice(1), sockPath);
  } else if (cmd === 'daemon') {
    exitCode = await cmdDaemon(argv.slice(1), sockPath);
  } else if (cmd === 'preflight') {
    exitCode = await cmdPreflight(argv.slice(1), sockPath);
  } else {
    throw new UsageError(`Unknown subcommand: ${cmd}`);
  }
  process.exit(exitCode);
}

main().catch((err) => {
  if (err instanceof UsageError) {
    process.stderr.write(`Error: ${err.message}\n`);
    process.exit(2);
  }
  if (err?.code === 'ENOENT_PI' || err?.data?.code === 'ENOENT_PI') {
    process.stderr.write(`Error: ${err.message}\n`);
    process.exit(127);
  }
  if (err?.rpcCode === -32602) {
    process.stderr.write(`Error: ${err.message}\n`);
    process.exit(2);
  }
  if (err?.code === 'ENOENT' || err?.code === 'ECONNREFUSED') {
    process.stderr.write(`Error: pi-rpc daemon is not running (${err.message})\n`);
    process.exit(1);
  }
  process.stderr.write(`Error: ${err?.message || String(err)}\n`);
  process.exit(1);
});
