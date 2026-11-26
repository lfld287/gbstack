import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { appendFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { JsonlDecoder } from './protocol.mjs';
import {
  captureSnapshot,
  collectDiffStat,
  detectGitWorktree,
  diffSnapshots,
  resolveWorkdir,
  verifyBaseline,
} from './workspace-guard.mjs';

const COMMAND_TIMEOUT_MS = 15000;
const STDERR_RING_BYTES = 4096;
const DEFAULT_MAX_SESSIONS = 8;
const DEFAULT_IDLE_TTL_MS = 30 * 60 * 1000;

function isExecutableFile(filePath) {
  try {
    const st = statSync(filePath);
    if (!st.isFile()) return false;
    // Check owner/group/other execute bit on POSIX
    return (st.mode & 0o111) !== 0;
  } catch {
    return false;
  }
}

function parseSemverTuple(name) {
  const m = /^v?(\d+)\.(\d+)\.(\d+)/.exec(name);
  if (!m) return null;
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

function compareVersionNamesDesc(a, b) {
  const va = parseSemverTuple(a);
  const vb = parseSemverTuple(b);
  if (va && vb) {
    for (let i = 0; i < 3; i += 1) {
      if (va[i] !== vb[i]) return vb[i] - va[i];
    }
  } else if (va) {
    return -1;
  } else if (vb) {
    return 1;
  }
  return b.localeCompare(a);
}

function findLatestGlobPi(baseDir, subPathSegments) {
  if (!existsSync(baseDir)) return null;
  let dirs;
  try {
    dirs = readdirSync(baseDir).sort(compareVersionNamesDesc);
  } catch {
    return null;
  }
  for (const d of dirs) {
    const candidate = path.join(baseDir, d, ...subPathSegments);
    if (isExecutableFile(candidate)) return candidate;
  }
  return null;
}

/**
 * Resolves the `pi` CLI binary without hardcoded personal paths.
 */
export function resolvePiBinary() {
  if (process.env.GBSTACK_PI_BIN) {
    if (isExecutableFile(process.env.GBSTACK_PI_BIN)) {
      return process.env.GBSTACK_PI_BIN;
    }
    const err = new Error(`GBSTACK_PI_BIN is not an executable file: ${process.env.GBSTACK_PI_BIN}`);
    err.code = 'ENOENT_PI';
    throw err;
  }

  for (const dir of (process.env.PATH || '').split(path.delimiter).filter(Boolean)) {
    const candidate = path.join(dir, 'pi');
    if (isExecutableFile(candidate)) return candidate;
  }

  const execSibling = path.join(path.dirname(process.execPath), 'pi');
  if (isExecutableFile(execSibling)) return execSibling;

  const home = homedir();
  const dynamicCandidates = [
    findLatestGlobPi(path.join(home, '.nvm', 'versions', 'node'), ['bin', 'pi']),
    path.join(home, '.volta', 'bin', 'pi'),
    findLatestGlobPi(path.join(home, '.fnm', 'node-versions'), ['installation', 'bin', 'pi']),
    findLatestGlobPi(path.join(home, '.local', 'share', 'fnm', 'node-versions'), ['installation', 'bin', 'pi']),
    '/opt/homebrew/bin/pi',
    '/usr/local/bin/pi',
    path.join(home, '.local', 'bin', 'pi'),
  ];

  for (const candidate of dynamicCandidates) {
    if (candidate && isExecutableFile(candidate)) return candidate;
  }

  const err = new Error(
    'pi executable not found (checked GBSTACK_PI_BIN, PATH, node sibling, nvm/volta/fnm/homebrew/local)'
  );
  err.code = 'ENOENT_PI';
  throw err;
}

function extractTextFromMessage(message) {
  if (!message || !Array.isArray(message.content)) return '';
  return message.content
    .filter((b) => b && b.type === 'text' && typeof b.text === 'string')
    .map((b) => b.text)
    .join('\n');
}

function addUsage(acc, u) {
  if (!u || typeof u !== 'object') return acc;
  const prev = acc || { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0 };
  return {
    input: prev.input + (u.input || 0),
    output: prev.output + (u.output || 0),
    cacheRead: prev.cacheRead + (u.cacheRead || 0),
    cacheWrite: prev.cacheWrite + (u.cacheWrite || 0),
    totalTokens: prev.totalTokens + (u.totalTokens || 0),
  };
}

function killProcessGroup(pid, signal) {
  if (!pid) return;
  try {
    process.kill(-pid, signal);
  } catch {
    try {
      process.kill(pid, signal);
    } catch {
      // ignore already exited
    }
  }
}

export class PiSession {
  constructor({ id, piBin, cwd, inGit, toplevel, thinking, readonly, model, log, onClose }) {
    this.id = id;
    this.cwd = cwd;
    this.inGit = inGit;
    this.toplevel = toplevel;
    this.thinking = thinking;
    this.readonly = readonly;
    this.model = model || null;
    this.log = log;
    this.onClose = onClose;

    this.state = 'idle'; // idle | busy | running | aborting | closing | crashed | closed
    this.lastActivityAt = Date.now();
    this.reqSeq = 0;
    this.pendingCommands = new Map();
    this.activeRun = null;
    this.stderrRing = '';
    this.closePromise = null;

    const args = [
      '--mode', 'rpc',
      '--no-session',
      '--no-context-files',
      '--no-mcp',
      '--no-extensions',
      '--no-skills',
      '--no-prompt-templates',
      '--no-themes',
      '--offline',
    ];
    if (this.model) {
      args.push('--model', this.model);
    }
    args.push('--thinking', this.thinking);
    if (this.readonly) {
      args.push('--tools', 'read,grep,find,ls');
    }

    const nodeDir = path.dirname(process.execPath);
    const currentPath = process.env.PATH || '';
    const envPath = currentPath.split(path.delimiter).includes(nodeDir)
      ? currentPath
      : `${nodeDir}${currentPath ? path.delimiter + currentPath : ''}`;

    this.log('spawning pi', { id: this.id, cwd: this.cwd, readonly: this.readonly, thinking: this.thinking, model: this.model });
    this.proc = spawn(piBin, args, {
      cwd: this.cwd,
      detached: true,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, PATH: envPath },
    });

    this.decoder = new JsonlDecoder({
      onRecord: (rec) => this.#handleRecord(rec),
      onError: (err, line) => this.log(`pi[${this.id}] parse error`, { err: err.message, line: line.slice(0, 200) }),
    });

    this.proc.stdout.on('data', (chunk) => this.decoder.push(chunk));
    this.proc.stdout.on('end', () => this.decoder.flush());

    this.proc.stderr.setEncoding('utf8');
    this.proc.stderr.on('data', (chunk) => {
      this.stderrRing += chunk;
      if (this.stderrRing.length > STDERR_RING_BYTES) {
        this.stderrRing = this.stderrRing.slice(-STDERR_RING_BYTES);
      }
    });

    this.proc.stdin.on('error', (err) => {
      this.log(`pi[${this.id}] stdin error`, { err: err.message });
    });

    this.proc.on('error', (err) => {
      this.log(`pi[${this.id}] proc error`, { err: err.message });
      this.#handleExit(null, null, err);
    });

    this.proc.on('exit', (code, signal) => {
      this.#handleExit(code, signal, null);
    });
  }

  touch() {
    this.lastActivityAt = Date.now();
  }

  #appendStderrSuffix() {
    const trimmed = this.stderrRing.trim();
    return trimmed ? ` | stderr: ${trimmed.slice(-512)}` : '';
  }

  #rejectPendingAndWaiters(reason) {
    for (const [, p] of this.pendingCommands) {
      clearTimeout(p.timer);
      p.reject(new Error(reason));
    }
    this.pendingCommands.clear();

    if (this.activeRun && !this.activeRun.settled) {
      this.activeRun.error = new Error(reason);
      this.activeRun.settled = true;
      this.activeRun.endedAt = Date.now();
      this.#notifyWaiters();
    }
  }

  #handleExit(code, signal, spawnErr) {
    if (this.state === 'closed') return;
    const wasClosing = this.state === 'closing';
    this.state = wasClosing ? 'closed' : 'crashed';
    this.log(`pi[${this.id}] exited`, { code, signal, state: this.state });

    const reason = spawnErr
      ? `pi spawn error: ${spawnErr.message}`
      : `pi process exited unexpectedly (code=${code}, signal=${signal})${this.#appendStderrSuffix()}`;

    this.#rejectPendingAndWaiters(reason);
    this.onClose?.(this);
  }

  sendCommand(cmd, timeoutMs = COMMAND_TIMEOUT_MS) {
    if (this.state === 'closing' || this.state === 'crashed' || this.state === 'closed') {
      return Promise.reject(new Error(`Session ${this.id} is ${this.state}`));
    }
    this.touch();
    const id = `cmd-${++this.reqSeq}`;
    const payload = { id, ...cmd };

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingCommands.delete(id);
        reject(new Error(`Command ${cmd.type} timed out after ${timeoutMs}ms`));
      }, timeoutMs);

      this.pendingCommands.set(id, { resolve, reject, timer });
      try {
        this.proc.stdin.write(JSON.stringify(payload) + '\n', (err) => {
          if (err) {
            const pending = this.pendingCommands.get(id);
            if (pending) {
              clearTimeout(pending.timer);
              this.pendingCommands.delete(id);
              pending.reject(err);
            }
          }
        });
      } catch (err) {
        clearTimeout(timer);
        this.pendingCommands.delete(id);
        reject(err);
      }
    });
  }

  #handleRecord(rec) {
    this.touch();
    if (rec.type === 'extension_ui_request' && rec.id) {
      try {
        this.proc.stdin.write(JSON.stringify({ type: 'extension_ui_response', id: rec.id, cancelled: true }) + '\n');
      } catch {
        // ignore if stdin already closed
      }
      return;
    }

    if (rec.type === 'response' && rec.id && this.pendingCommands.has(rec.id)) {
      const pending = this.pendingCommands.get(rec.id);
      this.pendingCommands.delete(rec.id);
      clearTimeout(pending.timer);
      if (rec.success) {
        pending.resolve(rec);
      } else {
        pending.reject(new Error(rec.error || `Command ${rec.command} failed`));
      }
      return;
    }

    const run = this.activeRun;
    if (!run || run.settled) return;

    if (rec.type === 'tool_execution_start' && rec.toolName) {
      run.toolsUsed.push(rec.toolName);
    } else if (rec.type === 'message_end' && rec.message) {
      if (rec.message.role === 'assistant') {
        const text = extractTextFromMessage(rec.message);
        if (text) run.lastReply = text;
        if (rec.message.usage) run.usage = addUsage(run.usage, rec.message.usage);
        if (rec.message.stopReason === 'error') {
          run.assistantError = rec.message.errorMessage || 'Assistant error';
        } else {
          run.assistantError = null;
        }
      }
    } else if (rec.type === 'agent_settled') {
      this.#settleRun(run, Boolean(rec.aborted));
    }
  }

  #settleRun(run, aborted) {
    if (run.settled) return;
    run.settled = true;
    run.aborted = aborted;
    run.endedAt = Date.now();

    try {
      const postSnapshot = captureSnapshot(this.cwd, this.inGit, this.toplevel);
      const { changed_files, out_of_scope_files } = diffSnapshots(run.preSnapshot, postSnapshot, run.allowedPaths);
      const readonly_violation = Boolean(this.readonly && changed_files.length > 0);
      const diff_stat = collectDiffStat(this.cwd, this.inGit);

      const status = run.aborted ? 'aborted' : run.assistantError ? 'error' : 'done';
      const result = {
        session_id: this.id,
        status,
        duration_ms: run.endedAt - run.startedAt,
        reply: run.lastReply,
        tools_used: run.toolsUsed,
        usage: run.usage,
        changed_files,
        out_of_scope_files,
        readonly_violation,
        diff_stat,
      };
      if (run.assistantError) {
        result.error = run.assistantError;
      }
      run.settledResult = result;
    } catch (err) {
      run.error = err instanceof Error ? err : new Error(String(err));
    } finally {
      if (this.state === 'running' || this.state === 'aborting') {
        this.state = 'idle';
      }
      this.#notifyWaiters();
    }
  }

  #notifyWaiters() {
    if (!this.activeRun) return;
    const waiters = this.activeRun.waiters.splice(0);
    for (const w of waiters) w();
  }

  async startPrompt(prompt, waitMs, allowedPaths, thinking, { signal } = {}) {
    if (this.state !== 'idle') {
      throw new Error(`Session ${this.id} is ${this.state}; cannot start prompt`);
    }

    if (thinking !== undefined && thinking !== this.thinking) {
      this.state = 'busy';
      try {
        await this.sendCommand({ type: 'set_thinking_level', level: thinking });
        this.thinking = thinking;
      } catch (err) {
        if (this.state === 'busy') this.state = 'idle';
        throw err;
      }
      if (this.state !== 'busy') {
        throw new Error(`Session ${this.id} is ${this.state}; cannot start prompt`);
      }
    }

    let preSnapshot;
    try {
      preSnapshot = captureSnapshot(this.cwd, this.inGit, this.toplevel);
    } catch (err) {
      if (this.state === 'busy') this.state = 'idle';
      throw err;
    }

    const run = {
      startedAt: Date.now(),
      endedAt: null,
      settled: false,
      aborted: false,
      error: null,
      assistantError: null,
      toolsUsed: [],
      lastReply: '',
      usage: null,
      waiters: [],
      preSnapshot,
      allowedPaths,
      settledResult: null,
    };
    this.activeRun = run;
    this.state = 'running';

    let resp;
    try {
      resp = await this.sendCommand({ type: 'prompt', message: prompt });
    } catch (err) {
      this.activeRun = null;
      if (this.state === 'running') {
        this.state = 'idle';
      }
      throw err;
    }

    if (resp.data?.disposition === 'handled') {
      this.#settleRun(run, false);
    }

    return this.waitForRun(waitMs, { signal });
  }

  async waitForRun(waitMs = 60000, { signal } = {}) {
    const run = this.activeRun;
    if (!run) {
      throw new Error(`Session ${this.id} has no active or completed run`);
    }
    const shouldWait = !run.settled && (waitMs === null || waitMs > 0) && !signal?.aborted;
    if (shouldWait) {
      await new Promise((resolve) => {
        let timer = null;
        const cleanup = () => {
          if (timer !== null) {
            clearTimeout(timer);
            timer = null;
          }
          if (signal) {
            signal.removeEventListener('abort', onAbort);
          }
        };
        const removeWaiter = () => {
          const idx = run.waiters.indexOf(done);
          if (idx !== -1) run.waiters.splice(idx, 1);
        };
        const done = () => {
          cleanup();
          resolve();
        };
        const onAbort = () => {
          removeWaiter();
          cleanup();
          resolve();
        };
        if (waitMs !== null) {
          timer = setTimeout(() => {
            removeWaiter();
            cleanup();
            resolve();
          }, waitMs);
        }
        if (signal) {
          signal.addEventListener('abort', onAbort, { once: true });
        }
        run.waiters.push(done);
      });
    }

    if (run.error) {
      throw run.error;
    }
    if (run.settled) {
      return run.settledResult;
    }
    return {
      session_id: this.id,
      status: 'running',
      elapsed_ms: Date.now() - run.startedAt,
      recent_tools: run.toolsUsed.slice(-10),
    };
  }

  async abort(waitMs = 10000, { signal } = {}) {
    if (this.state === 'idle') {
      return { session_id: this.id, status: 'idle' };
    }
    if (this.state !== 'running' && this.state !== 'aborting') {
      throw new Error(`Session ${this.id} cannot abort in state ${this.state}`);
    }
    this.state = 'aborting';
    const abortTimeoutMs = waitMs !== null && waitMs > 0 ? waitMs : COMMAND_TIMEOUT_MS;
    try {
      await this.sendCommand({ type: 'clear_queue' }, Math.min(COMMAND_TIMEOUT_MS, abortTimeoutMs));
    } catch {
      // Proceed to abort even if clear_queue fails
    }
    await this.sendCommand({ type: 'abort' }, abortTimeoutMs);
    return this.waitForRun(waitMs, { signal });
  }

  async reset() {
    if (this.state !== 'idle') {
      throw new Error(`Session ${this.id} must be idle to reset (current state: ${this.state})`);
    }
    this.state = 'busy';
    try {
      await this.sendCommand({ type: 'new_session' });
      this.activeRun = null;
      return { session_id: this.id, status: 'reset' };
    } finally {
      if (this.state === 'busy') {
        this.state = 'idle';
      }
    }
  }

  close() {
    if (this.closePromise) return this.closePromise;
    if (this.state === 'closed') return Promise.resolve();

    this.state = 'closing';
    this.#rejectPendingAndWaiters(`Session ${this.id} is closing`);

    this.closePromise = new Promise((resolve) => {
      let finished = false;
      let killTimer = null;
      const onExit = () => finish();
      const finish = () => {
        if (finished) return;
        finished = true;
        clearTimeout(killTimer);
        this.proc?.off('exit', onExit);
        this.state = 'closed';
        this.onClose?.(this);
        resolve();
      };

      if (!this.proc || this.proc.exitCode !== null || this.proc.signalCode !== null) {
        finish();
        return;
      }

      killTimer = setTimeout(() => {
        killProcessGroup(this.proc.pid, 'SIGKILL');
        finish();
      }, 3000);

      this.proc.once('exit', onExit);

      try {
        this.proc.stdin.end();
      } catch {
        // ignore
      }
      killProcessGroup(this.proc.pid, 'SIGTERM');
    });

    return this.closePromise;
  }
}

export class SessionPool {
  constructor() {
    this.sessions = new Map();
    this.writableCwdOwners = new Map();
    this.pendingSpawns = 0;
    this.maxSessions = Math.max(1, Number(process.env.GBSTACK_PI_MAX_SESSIONS) || DEFAULT_MAX_SESSIONS);
    this.idleTtlMs = Math.max(50, Number(process.env.GBSTACK_PI_IDLE_TTL_MS) || DEFAULT_IDLE_TTL_MS);

    const sweepInterval = Math.min(30000, Math.max(50, Math.floor(this.idleTtlMs / 2)));
    this.idleSweep = setInterval(() => this.#sweepIdle(), sweepInterval);
    this.idleSweep.unref();
  }

  log(msg, data) {
    const line = `[${new Date().toISOString()}] ${msg}${data !== undefined ? ' ' + JSON.stringify(data) : ''}\n`;
    const logPath = process.env.GBSTACK_PI_DAEMON_LOG || process.env.GBSTACK_PI_MCP_LOG;
    if (logPath) {
      try {
        appendFileSync(logPath, line);
        return;
      } catch {
        // fallback to stderr
      }
    }
    process.stderr.write(line);
  }

  #sweepIdle() {
    const now = Date.now();
    for (const sess of this.sessions.values()) {
      if (sess.state === 'idle' && now - sess.lastActivityAt > this.idleTtlMs) {
        this.log('evicting idle session (TTL)', { id: sess.id });
        sess.close();
      }
    }
  }

  #unregister(sess) {
    this.sessions.delete(sess.id);
    if (!sess.readonly && this.writableCwdOwners.get(sess.cwd) === sess.id) {
      this.writableCwdOwners.delete(sess.cwd);
    }
  }

  #activeSessionCount() {
    let count = this.pendingSpawns;
    for (const sess of this.sessions.values()) {
      if (sess.state !== 'closing' && sess.state !== 'closed' && sess.state !== 'crashed') {
        count += 1;
      }
    }
    return count;
  }

  #evictLruSlotSync() {
    if (this.#activeSessionCount() < this.maxSessions) return null;
    let lru = null;
    for (const sess of this.sessions.values()) {
      if (sess.state === 'idle') {
        if (!lru || sess.lastActivityAt < lru.lastActivityAt) {
          lru = sess;
        }
      }
    }
    if (!lru) {
      throw new Error(`Session pool at capacity (${this.maxSessions}) and no idle sessions available to evict`);
    }
    this.log('evicting LRU idle session', { id: lru.id });
    return lru.close();
  }

  getSession(sessionId) {
    const sess = this.sessions.get(sessionId);
    if (!sess || sess.state === 'crashed' || sess.state === 'closed') {
      throw new Error(`Session not found: ${sessionId}`);
    }
    return sess;
  }

  async run({
    cwd,
    prompt,
    session_id,
    new_session = false,
    thinking,
    readonly = false,
    model,
    wait_ms = 60000,
    baseline,
    signal,
    onSession,
  }) {
    const workdir = resolveWorkdir(cwd);
    const { inGit, toplevel } = detectGitWorktree(workdir);
    const allowedPaths = verifyBaseline(baseline, workdir, inGit);
    const requestedModel = model || process.env.GBSTACK_PI_MODEL || null;

    if (session_id) {
      const sess = this.getSession(session_id);
      if (sess.cwd !== workdir) {
        throw new Error(`Session ${session_id} cwd conflict: expected ${sess.cwd}, got ${workdir}`);
      }
      if (sess.readonly !== readonly) {
        throw new Error(`Session ${session_id} readonly conflict: expected ${sess.readonly}, got ${readonly}`);
      }
      if (model !== undefined && sess.model !== model) {
        throw new Error(`Session ${session_id} model conflict: expected ${sess.model}, got ${model}`);
      }
      onSession?.(sess);
      if (new_session) {
        await sess.reset();
      }
      if (signal?.aborted) {
        throw new Error('Run aborted before prompt start');
      }
      return sess.startPrompt(prompt, wait_ms, allowedPaths, thinking, { signal });
    }

    if (!readonly && this.writableCwdOwners.has(workdir)) {
      const ownerId = this.writableCwdOwners.get(workdir);
      const existing = this.sessions.get(ownerId);
      if (!existing) {
        throw new Error(`Workspace ${workdir} already has an active writable session (${ownerId})`);
      }
      if (existing.state === 'closed' || existing.state === 'crashed') {
        this.writableCwdOwners.delete(workdir);
      } else {
        if (existing.state !== 'idle') {
          throw new Error(`Workspace ${workdir} already has an active writable session (${ownerId})`);
        }
        if (existing.model === requestedModel) {
          existing.state = 'busy';
          try {
            await existing.sendCommand({ type: 'new_session' });
          } catch (err) {
            if (existing.state === 'busy') existing.state = 'idle';
            throw err;
          }
          if (existing.state !== 'busy') {
            throw new Error(`Session ${ownerId} is ${existing.state}; cannot recycle`);
          }
          const newId = `pi-${randomUUID().slice(0, 8)}`;
          this.sessions.delete(ownerId);
          existing.id = newId;
          existing.activeRun = null;
          this.sessions.set(newId, existing);
          this.writableCwdOwners.set(workdir, newId);
          existing.state = 'idle';
          this.log('recycled idle writable session with new_session', { oldId: ownerId, newId, cwd: workdir });
          onSession?.(existing);
          if (signal?.aborted) {
            throw new Error('Run aborted before prompt start');
          }
          return existing.startPrompt(prompt, wait_ms, allowedPaths, thinking ?? 'medium', { signal });
        }
        await existing.close();
        if (this.writableCwdOwners.has(workdir)) {
          const raceOwner = this.writableCwdOwners.get(workdir);
          throw new Error(`Workspace ${workdir} already has an active writable session (${raceOwner})`);
        }
      }
    }

    const piBin = resolvePiBinary();
    const evictPromise = this.#evictLruSlotSync();
    const id = `pi-${randomUUID().slice(0, 8)}`;
    this.pendingSpawns += 1;
    let reserved = true;
    if (!readonly) {
      this.writableCwdOwners.set(workdir, id);
    }

    let sess = null;
    try {
      if (evictPromise) {
        await evictPromise;
      }
      if (signal?.aborted) {
        throw new Error('Run aborted before session spawn');
      }
      sess = new PiSession({
        id,
        piBin,
        cwd: workdir,
        inGit,
        toplevel,
        thinking: thinking ?? 'medium',
        readonly,
        model: requestedModel,
        log: (m, d) => this.log(m, d),
        onClose: (s) => this.#unregister(s),
      });
      this.sessions.set(id, sess);
      this.pendingSpawns -= 1;
      reserved = false;
      onSession?.(sess);
      return await sess.startPrompt(prompt, wait_ms, allowedPaths, undefined, { signal });
    } catch (err) {
      if (sess) {
        await sess.close();
      } else if (!readonly && this.writableCwdOwners.get(workdir) === id) {
        this.writableCwdOwners.delete(workdir);
      }
      throw err;
    } finally {
      if (reserved) {
        this.pendingSpawns -= 1;
      }
    }
  }

  listSessions() {
    const list = [];
    for (const s of this.sessions.values()) {
      const entry = {
        session_id: s.id,
        pid: s.proc?.pid ?? null,
        cwd: s.cwd,
        state: s.state,
        thinking: s.thinking,
        readonly: s.readonly,
        model: s.model,
        last_activity_ms_ago: Date.now() - s.lastActivityAt,
      };
      if ((s.state === 'running' || s.state === 'aborting') && s.activeRun && !s.activeRun.settled) {
        entry.active_run = {
          elapsed_ms: Date.now() - s.activeRun.startedAt,
          tools_count: s.activeRun.toolsUsed.length,
          recent_tools: s.activeRun.toolsUsed.slice(-10),
        };
      }
      list.push(entry);
    }
    return { sessions: list };
  }

  async closeSession(sessionId) {
    if (sessionId) {
      const sess = this.sessions.get(sessionId);
      if (!sess) throw new Error(`Session not found: ${sessionId}`);
      await sess.close();
      return { closed: [sessionId] };
    }
    const ids = Array.from(this.sessions.keys());
    await this.closeAllSessions();
    return { closed: ids };
  }

  async closeAllSessions() {
    const all = Array.from(this.sessions.values());
    await Promise.all(all.map((s) => s.close()));
  }

  async closeAll() {
    clearInterval(this.idleSweep);
    await this.closeAllSessions();
  }
}
