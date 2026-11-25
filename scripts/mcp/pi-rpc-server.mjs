import {
  encodeJsonRpcFrame,
  INVALID_PARAMS,
  INVALID_REQUEST,
  JsonlDecoder,
  jsonRpcError,
  jsonRpcResult,
  METHOD_NOT_FOUND,
  PARSE_ERROR,
  truncateUtf8,
} from './protocol.mjs';
import { SessionPool } from './session-pool.mjs';

const MAX_REPLY_BYTES = 64 * 1024;
const MAX_FRAME_BYTES = 512 * 1024;
const THINKING_LEVELS = new Set(['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']);

const SERVER_INSTRUCTIONS =
  'Persistent pi RPC worker pool. Use pi_run to start or reuse a session (pass readonly:true for review/read-only work, baseline for git/file/allowedPaths guards). ' +
  'If pi_run returns status="running", poll with pi_wait, redirect with pi_steer, or cancel with pi_abort. Use pi_sessions to list, reset (new_session), or close sessions.';

const TOOLS = [
  {
    name: 'pi_run',
    description:
      'Run a prompt in a persistent pi RPC session. Reuses session_id if provided, or spawns a new session bound to cwd.',
    inputSchema: {
      type: 'object',
      properties: {
        cwd: { type: 'string', description: 'Working directory for the pi session' },
        prompt: { type: 'string', description: 'Prompt to send to pi' },
        session_id: { type: 'string', description: 'Optional existing session_id to reuse' },
        thinking: {
          type: 'string',
          enum: [...THINKING_LEVELS],
          description: 'Thinking level (default: medium)',
        },
        readonly: { type: 'boolean', description: 'Restrict pi to read-only tools (read,grep,find,ls)' },
        model: { type: 'string', description: 'Optional model override (defaults to user pi settings)' },
        wait_ms: { type: 'number', description: 'Max ms to wait for completion (default: 60000)' },
        baseline: {
          type: 'object',
          description: 'Optional 4-key baseline guard: gitCleanHead, fileSha1, plannedNew, allowedPaths',
          properties: {
            gitCleanHead: { type: 'string' },
            fileSha1: { type: 'object' },
            plannedNew: { type: 'array', items: { type: 'string' } },
            allowedPaths: { type: 'array', items: { type: 'string' } },
          },
          additionalProperties: false,
        },
      },
      required: ['cwd', 'prompt'],
    },
  },
  {
    name: 'pi_wait',
    description: 'Wait for an active pi session turn to settle or return progress after wait_ms (idempotent after completion).',
    inputSchema: {
      type: 'object',
      properties: {
        session_id: { type: 'string', description: 'Session ID returned by pi_run' },
        wait_ms: { type: 'number', description: 'Max ms to wait (default: 60000)' },
      },
      required: ['session_id'],
    },
  },
  {
    name: 'pi_steer',
    description: 'Send a steering message to a currently running pi session.',
    inputSchema: {
      type: 'object',
      properties: {
        session_id: { type: 'string', description: 'Session ID' },
        message: { type: 'string', description: 'Steering instruction' },
      },
      required: ['session_id', 'message'],
    },
  },
  {
    name: 'pi_abort',
    description: 'Clear queued messages and abort the current turn in a pi session, waiting for agent_settled.',
    inputSchema: {
      type: 'object',
      properties: {
        session_id: { type: 'string', description: 'Session ID' },
        wait_ms: { type: 'number', description: 'Max ms to wait for settle (default: 10000)' },
      },
      required: ['session_id'],
    },
  },
  {
    name: 'pi_sessions',
    description: 'List active pi sessions, reset a session context (new_session), or close sessions.',
    inputSchema: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['list', 'close', 'reset'], description: 'Action to perform' },
        session_id: { type: 'string', description: 'Session ID (required for reset, optional for close)' },
      },
      required: ['action'],
    },
  },
];

const pool = new SessionPool();

function writeFrame(obj) {
  process.stdout.write(encodeJsonRpcFrame(obj));
}

function formatBoundedToolResult(id, payload) {
  const out = { ...payload };
  if (typeof out.reply === 'string') {
    out.reply = truncateUtf8(out.reply, MAX_REPLY_BYTES);
  }

  let frame = jsonRpcResult(id, { content: [{ type: 'text', text: JSON.stringify(out) }] });
  if (Buffer.byteLength(encodeJsonRpcFrame(frame), 'utf8') < MAX_FRAME_BYTES) {
    return frame;
  }

  if (typeof out.reply === 'string') {
    out.reply = truncateUtf8(out.reply, 8192);
  }
  if (typeof out.diff_stat === 'string') {
    out.diff_stat = truncateUtf8(out.diff_stat, 4096);
  }
  for (const key of ['changed_files', 'out_of_scope_files', 'tools_used']) {
    if (Array.isArray(out[key]) && out[key].length > 200) {
      out[key] = out[key].slice(0, 200);
    }
  }
  frame = jsonRpcResult(id, { content: [{ type: 'text', text: JSON.stringify(out) }] });
  if (Buffer.byteLength(encodeJsonRpcFrame(frame), 'utf8') < MAX_FRAME_BYTES) {
    return frame;
  }

  if (typeof out.reply === 'string') {
    out.reply = truncateUtf8(out.reply, 1024);
  }
  if (typeof out.diff_stat === 'string') {
    out.diff_stat = '';
  }
  for (const key of ['changed_files', 'out_of_scope_files', 'tools_used']) {
    if (Array.isArray(out[key]) && out[key].length > 50) {
      out[key] = out[key].slice(0, 50);
    }
  }
  frame = jsonRpcResult(id, { content: [{ type: 'text', text: JSON.stringify(out) }] });
  if (Buffer.byteLength(encodeJsonRpcFrame(frame), 'utf8') < MAX_FRAME_BYTES) {
    return frame;
  }

  for (const key of ['changed_files', 'out_of_scope_files', 'tools_used']) {
    if (Array.isArray(out[key])) {
      out[key] = [];
    }
  }
  if (typeof out.reply === 'string') {
    out.reply = truncateUtf8(out.reply, 256);
  }
  return jsonRpcResult(id, { content: [{ type: 'text', text: JSON.stringify(out) }] });
}

class InvalidParamsError extends Error {}

function assertNonEmptyString(val, name) {
  if (typeof val !== 'string' || !val) {
    throw new InvalidParamsError(`${name} must be a non-empty string`);
  }
}

function assertWaitMs(val) {
  if (val !== undefined && (typeof val !== 'number' || !Number.isFinite(val) || val < 0)) {
    throw new InvalidParamsError('wait_ms must be a finite non-negative number');
  }
}

async function dispatchTool(name, args) {
  if (args === undefined || args === null) args = {};
  if (typeof args !== 'object' || Array.isArray(args)) {
    throw new InvalidParamsError('tools/call arguments must be an object');
  }

  if (name === 'pi_run') {
    assertNonEmptyString(args.cwd, 'cwd');
    assertNonEmptyString(args.prompt, 'prompt');
    assertWaitMs(args.wait_ms);
    if (args.thinking !== undefined && (typeof args.thinking !== 'string' || !THINKING_LEVELS.has(args.thinking))) {
      throw new InvalidParamsError(`thinking must be one of: ${[...THINKING_LEVELS].join(', ')}`);
    }
    if (args.readonly !== undefined && typeof args.readonly !== 'boolean') {
      throw new InvalidParamsError('readonly must be a boolean');
    }
    if (args.session_id !== undefined) {
      assertNonEmptyString(args.session_id, 'session_id');
    }
    if (args.model !== undefined) {
      assertNonEmptyString(args.model, 'model');
    }
    return pool.run(args);
  }

  if (name === 'pi_wait') {
    assertNonEmptyString(args.session_id, 'session_id');
    assertWaitMs(args.wait_ms);
    return pool.getSession(args.session_id).waitForRun(args.wait_ms ?? 60000);
  }

  if (name === 'pi_steer') {
    assertNonEmptyString(args.session_id, 'session_id');
    assertNonEmptyString(args.message, 'message');
    const sess = pool.getSession(args.session_id);
    if (sess.state !== 'running') {
      throw new Error(`Session ${args.session_id} is ${sess.state}; pi_steer requires a running session (use pi_run instead)`);
    }
    const resp = await sess.sendCommand({ type: 'steer', message: args.message });
    return { session_id: args.session_id, status: 'steered', disposition: resp.data?.disposition || 'queued' };
  }

  if (name === 'pi_abort') {
    assertNonEmptyString(args.session_id, 'session_id');
    assertWaitMs(args.wait_ms);
    return pool.getSession(args.session_id).abort(args.wait_ms ?? 10000);
  }

  if (name === 'pi_sessions') {
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
    throw new InvalidParamsError(`Unknown action: ${args.action}`);
  }

  throw new InvalidParamsError(`Unknown tool: ${name}`);
}

async function handleMcpRequest(msg) {
  if (!msg || typeof msg !== 'object' || Array.isArray(msg)) {
    writeFrame(jsonRpcError(null, INVALID_REQUEST, 'Invalid JSON-RPC request'));
    return;
  }

  const { id, method, params } = msg;
  if (typeof method !== 'string' || !method) {
    writeFrame(jsonRpcError(id ?? null, INVALID_REQUEST, 'Invalid JSON-RPC request: method must be a non-empty string'));
    return;
  }
  if (id === undefined || id === null) {
    // Notification (e.g. notifications/initialized)
    return;
  }

  if (method === 'initialize') {
    writeFrame(
      jsonRpcResult(id, {
        protocolVersion: params?.protocolVersion || '2024-11-05',
        capabilities: { tools: {} },
        serverInfo: { name: 'pi-rpc-mcp', version: '1.0.0' },
        instructions: SERVER_INSTRUCTIONS,
      })
    );
    return;
  }

  if (method === 'ping') {
    writeFrame(jsonRpcResult(id, {}));
    return;
  }

  if (method === 'tools/list') {
    writeFrame(jsonRpcResult(id, { tools: TOOLS }));
    return;
  }

  if (method === 'tools/call') {
    if (!params || typeof params !== 'object' || typeof params.name !== 'string') {
      writeFrame(jsonRpcError(id, INVALID_PARAMS, 'tools/call requires params.name'));
      return;
    }
    try {
      const result = await dispatchTool(params.name, params.arguments);
      writeFrame(formatBoundedToolResult(id, result));
    } catch (err) {
      if (err instanceof InvalidParamsError) {
        writeFrame(jsonRpcError(id, INVALID_PARAMS, err.message));
        return;
      }
      pool.log('tools/call error', { tool: params.name, error: err.message });
      writeFrame(
        jsonRpcResult(id, {
          isError: true,
          content: [{ type: 'text', text: JSON.stringify({ error: err.message }) }],
        })
      );
    }
    return;
  }

  writeFrame(jsonRpcError(id, METHOD_NOT_FOUND, `Method not found: ${method}`));
}

const stdinDecoder = new JsonlDecoder({
  onRecord: (msg) => {
    handleMcpRequest(msg).catch((err) => {
      pool.log('unhandled mcp request error', { error: err?.message || String(err) });
    });
  },
  onError: (err) => {
    pool.log('mcp stdin parse error', { error: err.message });
    writeFrame(jsonRpcError(null, PARSE_ERROR, `Parse error: ${err.message}`));
  },
});

process.stdin.on('data', (chunk) => stdinDecoder.push(chunk));

let shuttingDown = false;
async function shutdown(reason) {
  if (shuttingDown) return;
  shuttingDown = true;
  pool.log('shutting down', { reason, sessions: pool.sessions.size });
  await pool.closeAll();
  process.exit(0);
}

process.stdin.on('end', () => {
  stdinDecoder.flush();
  shutdown('stdin_end');
});
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

pool.log('pi-rpc-mcp server started', { pid: process.pid });
