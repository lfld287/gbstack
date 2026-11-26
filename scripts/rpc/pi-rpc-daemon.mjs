import {
  chmodSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  encodeJsonRpcFrame,
  INVALID_PARAMS,
  INVALID_REQUEST,
  JsonlDecoder,
  jsonRpcError,
  jsonRpcResult,
  METHOD_NOT_FOUND,
  PARSE_ERROR,
} from './protocol.mjs';
import { SessionPool } from './session-pool.mjs';
import { ValidationError } from './workspace-guard.mjs';

const THINKING_LEVELS = new Set(['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']);
const DEFAULT_DAEMON_IDLE_TTL_MS = 30 * 60 * 1000;

export function resolveSocketPath() {
  if (process.env.GBSTACK_PI_RPC_SOCK) {
    return path.resolve(process.env.GBSTACK_PI_RPC_SOCK);
  }
  const xdg = process.env.XDG_RUNTIME_DIR;
  if (xdg) {
    const candidate = path.join(xdg, 'gbstack-pi-rpc', 'daemon.sock');
    if (Buffer.byteLength(candidate, 'utf8') <= 100) {
      return candidate;
    }
  }
  return `/tmp/gbstack-pi-rpc-${process.getuid()}/daemon.sock`;
}

export function ensureSecureSockDir(sockDir) {
  mkdirSync(sockDir, { recursive: true, mode: 0o700 });
  const st = lstatSync(sockDir);
  if (st.isSymbolicLink() || !st.isDirectory()) {
    throw new Error(`Socket directory must be a real directory, not a symlink: ${sockDir}`);
  }
  if (st.uid !== process.getuid()) {
    throw new Error(`Socket directory owner mismatch (${st.uid} !== ${process.getuid()}): ${sockDir}`);
  }
  if ((st.mode & 0o077) !== 0) {
    throw new Error(
      `Socket directory permissions too permissive (${(st.mode & 0o777).toString(8)}): ${sockDir}`
    );
  }
}

class InvalidParamsError extends Error {}

function assertNonEmptyString(val, name) {
  if (typeof val !== 'string' || !val) {
    throw new InvalidParamsError(`${name} must be a non-empty string`);
  }
}

function assertOptionalWaitMs(val) {
  if (val !== undefined && val !== null && (typeof val !== 'number' || !Number.isFinite(val) || val < 0)) {
    throw new InvalidParamsError('wait_ms must be null or a finite non-negative number');
  }
}

function parseDaemonArgs(argv) {
  let socketPath = null;
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--socket' && i + 1 < argv.length) {
      socketPath = path.resolve(argv[i + 1]);
      i += 1;
    }
  }
  return { socketPath: socketPath || resolveSocketPath() };
}

function writeFrame(socket, obj) {
  if (socket.destroyed) return;
  try {
    socket.write(encodeJsonRpcFrame(obj));
  } catch {
    // ignore write after close
  }
}

export function startDaemon(sockPath = resolveSocketPath()) {
  const sockDir = path.dirname(sockPath);
  const pidPath = path.join(sockDir, 'daemon.pid');
  const startedAt = Date.now();
  const daemonIdleTtlMs = Math.max(
    50,
    Number(process.env.GBSTACK_PI_DAEMON_IDLE_TTL_MS) || DEFAULT_DAEMON_IDLE_TTL_MS
  );

  process.umask(0o077);
  ensureSecureSockDir(sockDir);

  const pool = new SessionPool();
  const activeClients = new Set();
  let lastActivityAt = Date.now();
  let ownsSocket = false;
  let shuttingDown = false;
  let server = null;

  const touchDaemon = () => {
    lastActivityAt = Date.now();
  };

  const cleanupFiles = () => {
    if (!ownsSocket) return;
    ownsSocket = false;
    try {
      const raw = readFileSync(pidPath, 'utf8');
      const info = JSON.parse(raw);
      if (info && info.pid && info.pid !== process.pid) {
        return;
      }
    } catch {
      // If pidPath is missing/unreadable, still clean up our socket if needed
    }
    try {
      unlinkSync(sockPath);
    } catch {
      // ignore ENOENT
    }
    try {
      unlinkSync(pidPath);
    } catch {
      // ignore ENOENT
    }
  };

  const shutdown = async (reason) => {
    if (shuttingDown) return;
    shuttingDown = true;
    clearInterval(idleSweepTimer);
    pool.log('daemon shutting down', { reason, sessions: pool.sessions.size });
    if (server) {
      try {
        server.close();
      } catch {
        // ignore
      }
    }
    for (const sock of activeClients) {
      try {
        sock.destroy();
      } catch {
        // ignore
      }
    }
    activeClients.clear();
    try {
      await pool.closeAll();
    } finally {
      cleanupFiles();
    }
    process.exit(0);
  };

  const idleCheckInterval = Math.min(30000, Math.max(50, Math.floor(daemonIdleTtlMs / 2)));
  const idleSweepTimer = setInterval(() => {
    if (pool.sessions.size > 0 || pool.pendingSpawns > 0 || activeClients.size > 0) {
      touchDaemon();
      return;
    }
    if (Date.now() - lastActivityAt >= daemonIdleTtlMs) {
      shutdown('idle_ttl');
    }
  }, idleCheckInterval);
  idleSweepTimer.unref();

  async function handleMethod(method, params, id, socket, inFlightRuns) {
    touchDaemon();
    const args = params === undefined || params === null ? {} : params;
    if (typeof args !== 'object' || Array.isArray(args)) {
      throw new InvalidParamsError('params must be an object');
    }

    if (method === 'ping') {
      return {
        ok: true,
        pid: process.pid,
        uptime_ms: Date.now() - startedAt,
        socket: sockPath,
      };
    }

    if (method === 'status') {
      return {
        pid: process.pid,
        uptime_ms: Date.now() - startedAt,
        socket: sockPath,
        sessions_count: pool.sessions.size,
        max_sessions: pool.maxSessions,
        idle_ttl_ms: pool.idleTtlMs,
        daemon_idle_ttl_ms: daemonIdleTtlMs,
        sessions: pool.listSessions().sessions,
      };
    }

    if (method === 'stop') {
      const closed = Array.from(pool.sessions.keys());
      setImmediate(() => shutdown('stop_rpc'));
      return { stopped: true, closed };
    }

    if (method === 'run') {
      assertNonEmptyString(args.cwd, 'cwd');
      assertNonEmptyString(args.prompt, 'prompt');
      assertOptionalWaitMs(args.wait_ms);
      if (args.thinking !== undefined && (typeof args.thinking !== 'string' || !THINKING_LEVELS.has(args.thinking))) {
        throw new InvalidParamsError(`thinking must be one of: ${[...THINKING_LEVELS].join(', ')}`);
      }
      if (args.readonly !== undefined && typeof args.readonly !== 'boolean') {
        throw new InvalidParamsError('readonly must be a boolean');
      }
      if (args.new_session !== undefined && typeof args.new_session !== 'boolean') {
        throw new InvalidParamsError('new_session must be a boolean');
      }
      if (args.abort_on_disconnect !== undefined && typeof args.abort_on_disconnect !== 'boolean') {
        throw new InvalidParamsError('abort_on_disconnect must be a boolean');
      }
      if (args.session_id !== undefined) {
        assertNonEmptyString(args.session_id, 'session_id');
      }
      if (args.model !== undefined) {
        assertNonEmptyString(args.model, 'model');
      }

      const abortOnDisconnect = args.abort_on_disconnect !== undefined ? args.abort_on_disconnect : true;
      const waitMs = args.wait_ms === undefined ? null : args.wait_ms;
      const entry = {
        abortController: new AbortController(),
        session: null,
        abortOnDisconnect,
      };
      inFlightRuns.add(entry);
      try {
        return await pool.run({
          ...args,
          wait_ms: waitMs,
          signal: entry.abortController.signal,
          onSession: (sess) => {
            entry.session = sess;
            if (!socket.destroyed) {
              writeFrame(socket, {
                jsonrpc: '2.0',
                method: 'run_started',
                params: { id, session_id: sess.id },
              });
            }
          },
        });
      } finally {
        inFlightRuns.delete(entry);
        touchDaemon();
      }
    }

    if (method === 'steer') {
      assertNonEmptyString(args.session_id, 'session_id');
      assertNonEmptyString(args.message, 'message');
      const sess = pool.getSession(args.session_id);
      if (sess.state !== 'running') {
        throw new Error(
          `Session ${args.session_id} is ${sess.state}; steer requires a running session`
        );
      }
      const resp = await sess.sendCommand({ type: 'steer', message: args.message });
      return {
        session_id: args.session_id,
        status: 'steered',
        disposition: resp.data?.disposition || 'queued',
      };
    }

    if (method === 'abort') {
      assertNonEmptyString(args.session_id, 'session_id');
      assertOptionalWaitMs(args.wait_ms);
      return pool.getSession(args.session_id).abort(args.wait_ms ?? 10000);
    }

    if (method === 'sessions') {
      assertNonEmptyString(args.action, 'action');
      if (args.action === 'list') return pool.listSessions();
      if (args.action === 'close') {
        if (args.session_id !== undefined) assertNonEmptyString(args.session_id, 'session_id');
        return pool.closeSession(args.session_id);
      }
      if (args.action === 'reset') {
        assertNonEmptyString(args.session_id, 'session_id');
        return pool.getSession(args.session_id).reset();
      }
      throw new InvalidParamsError(`Unknown sessions action: ${args.action}`);
    }

    const err = new Error(`Method not found: ${method}`);
    err.isMethodNotFound = true;
    throw err;
  }

  server = net.createServer((socket) => {
    activeClients.add(socket);
    touchDaemon();
    const inFlightRuns = new Set();

    const decoder = new JsonlDecoder({
      onRecord: (msg) => {
        if (!msg || typeof msg !== 'object' || Array.isArray(msg)) {
          writeFrame(socket, jsonRpcError(null, INVALID_REQUEST, 'Invalid JSON-RPC request'));
          return;
        }
        const { id, method, params } = msg;
        if (typeof method !== 'string' || !method) {
          writeFrame(
            socket,
            jsonRpcError(id ?? null, INVALID_REQUEST, 'Invalid JSON-RPC request: method must be a non-empty string')
          );
          return;
        }
        if (id === undefined || id === null) {
          return;
        }

        handleMethod(method, params, id, socket, inFlightRuns)
          .then((result) => {
            writeFrame(socket, jsonRpcResult(id, result));
          })
          .catch((err) => {
            if (err instanceof InvalidParamsError || err instanceof ValidationError || err?.code === 'INVALID_PARAMS') {
              writeFrame(socket, jsonRpcError(id, INVALID_PARAMS, err.message));
              return;
            }
            if (err?.isMethodNotFound) {
              writeFrame(socket, jsonRpcError(id, METHOD_NOT_FOUND, err.message));
              return;
            }
            pool.log('daemon rpc error', { method, error: err?.message || String(err) });
            const data = err?.code ? { code: err.code } : undefined;
            writeFrame(socket, jsonRpcError(id, -32000, err?.message || String(err), data));
          });
      },
      onError: (err) => {
        writeFrame(socket, jsonRpcError(null, PARSE_ERROR, `Parse error: ${err.message}`));
      },
    });

    socket.on('data', (chunk) => decoder.push(chunk));
    socket.on('end', () => decoder.flush());
    socket.on('error', (err) => {
      pool.log('client socket error', { error: err.message });
    });
    socket.on('close', () => {
      activeClients.delete(socket);
      touchDaemon();
      for (const entry of inFlightRuns) {
        if (!entry.abortOnDisconnect) continue;
        entry.abortController.abort();
        const sess = entry.session;
        if (sess) {
          if (sess.state === 'running' || sess.state === 'aborting') {
            sess
              .abort(5000)
              .then((res) => {
                if (res && res.status === 'running') {
                  pool.log('disconnect abort timed out; closing session', { id: sess.id });
                  return sess.close();
                }
              })
              .catch((err) => {
                pool.log('disconnect abort error; closing session', { id: sess.id, error: err.message });
                return sess.close();
              });
          } else if (sess.state === 'busy') {
            sess.close().catch(() => {});
          }
        }
      }
    });
  });

  server.on('error', (err) => {
    pool.log('daemon server error', { error: err.message });
    process.exit(1);
  });

  server.listen(sockPath, () => {
    chmodSync(sockPath, 0o600);
    const pidPayload = {
      pid: process.pid,
      startedAt: new Date(startedAt).toISOString(),
      socket: sockPath,
      nodeBin: process.execPath,
    };
    writeFileSync(pidPath, JSON.stringify(pidPayload) + '\n', { mode: 0o600 });
    chmodSync(pidPath, 0o600);
    ownsSocket = true;
    pool.log('pi-rpc daemon started', { pid: process.pid, socket: sockPath });
  });

  process.on('SIGHUP', () => {});
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('uncaughtException', (err) => {
    pool.log('uncaughtException', { error: err?.stack || String(err) });
    shutdown('uncaughtException');
  });
  process.on('unhandledRejection', (err) => {
    pool.log('unhandledRejection', { error: err?.stack || String(err) });
  });
}

function isMainModule() {
  if (!process.argv[1]) return false;
  try {
    return pathToFileURL(realpathSync(process.argv[1])).href === import.meta.url;
  } catch {
    return false;
  }
}

if (isMainModule()) {
  const { socketPath } = parseDaemonArgs(process.argv.slice(2));
  startDaemon(socketPath);
}
