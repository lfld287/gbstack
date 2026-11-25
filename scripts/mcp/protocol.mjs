import { StringDecoder } from 'node:string_decoder';

export const PARSE_ERROR = -32700;
export const INVALID_REQUEST = -32600;
export const METHOD_NOT_FOUND = -32601;
export const INVALID_PARAMS = -32602;

/**
 * Strict LF-only incremental JSONL decoder.
 * Never splits on U+2028/U+2029 and enforces maxLineBytes to bound memory.
 */
export class JsonlDecoder {
  constructor({ maxLineBytes = 2 * 1024 * 1024, onRecord, onError } = {}) {
    this.maxLineBytes = maxLineBytes;
    this.onRecord = onRecord;
    this.onError = onError;
    this.decoder = new StringDecoder('utf8');
    this.buf = '';
    this.discarding = false;
  }

  push(chunk) {
    const text = typeof chunk === 'string' ? chunk : this.decoder.write(chunk);
    if (!text) return;

    let start = 0;
    while (start < text.length) {
      const nl = text.indexOf('\n', start);
      if (nl === -1) {
        const tail = text.slice(start);
        if (!this.discarding) {
          this.buf += tail;
          if (Buffer.byteLength(this.buf, 'utf8') > this.maxLineBytes) {
            const err = new Error(`JSONL line exceeded maxLineBytes (${this.maxLineBytes})`);
            this.buf = '';
            this.discarding = true;
            this.onError?.(err, '');
          }
        }
        break;
      }

      const segment = text.slice(start, nl);
      start = nl + 1;

      if (this.discarding) {
        this.discarding = false;
        continue;
      }

      let line = this.buf + segment;
      this.buf = '';
      if (Buffer.byteLength(line, 'utf8') > this.maxLineBytes) {
        this.onError?.(new Error(`JSONL line exceeded maxLineBytes (${this.maxLineBytes})`), '');
        continue;
      }
      if (line.endsWith('\r')) line = line.slice(0, -1);
      if (!line.trim()) continue;
      this.#emitLine(line);
    }
  }

  flush() {
    const rem = this.decoder.end();
    if (rem && !this.discarding) {
      this.buf += rem;
    }
    if (this.discarding) {
      this.buf = '';
      this.discarding = false;
      return;
    }
    let line = this.buf;
    this.buf = '';
    if (line.endsWith('\r')) line = line.slice(0, -1);
    if (!line.trim()) return;
    if (Buffer.byteLength(line, 'utf8') > this.maxLineBytes) {
      this.onError?.(new Error(`JSONL line exceeded maxLineBytes (${this.maxLineBytes})`), '');
      return;
    }
    this.#emitLine(line);
  }

  #emitLine(line) {
    let parsed;
    try {
      parsed = JSON.parse(line);
    } catch (err) {
      this.onError?.(err, line);
      return;
    }
    this.onRecord?.(parsed);
  }
}

function sliceUtf8Head(buf, maxBytes) {
  if (maxBytes <= 0) return '';
  let end = Math.min(buf.length, maxBytes);
  while (end > 0 && (buf[end] & 0xc0) === 0x80) {
    end -= 1;
  }
  return buf.subarray(0, end).toString('utf8');
}

function sliceUtf8Tail(buf, maxBytes) {
  if (maxBytes <= 0) return '';
  let start = Math.max(0, buf.length - maxBytes);
  while (start < buf.length && (buf[start] & 0xc0) === 0x80) {
    start += 1;
  }
  return buf.subarray(start).toString('utf8');
}

/**
 * Truncates text to at most maxBytes of UTF-8, preserving head and tail.
 */
export function truncateUtf8(text, maxBytes) {
  const str = typeof text === 'string' ? text : String(text ?? '');
  if (maxBytes <= 0) return '';
  const raw = Buffer.from(str, 'utf8');
  if (raw.length <= maxBytes) return str;

  const omitted = raw.length - maxBytes;
  const marker = Buffer.from(`\n... [truncated ${omitted} bytes] ...\n`, 'utf8');
  if (maxBytes <= marker.length) {
    return sliceUtf8Head(raw, maxBytes);
  }
  const budget = maxBytes - marker.length;
  const headBudget = Math.floor(budget / 2);
  const tailBudget = budget - headBudget;
  return sliceUtf8Head(raw, headBudget) + marker.toString('utf8') + sliceUtf8Tail(raw, tailBudget);
}

export function encodeJsonRpcFrame(obj) {
  return JSON.stringify(obj) + '\n';
}

export function jsonRpcResult(id, result) {
  return { jsonrpc: '2.0', id, result };
}

export function jsonRpcError(id, code, message, data) {
  const error = data !== undefined ? { code, message, data } : { code, message };
  return { jsonrpc: '2.0', id: id ?? null, error };
}
