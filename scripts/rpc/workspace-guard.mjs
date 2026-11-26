import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  closeSync,
  lstatSync,
  openSync,
  readdirSync,
  readlinkSync,
  readSync,
  realpathSync,
  statSync,
} from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { truncateUtf8 } from './protocol.mjs';

const ALLOWED_BASELINE_KEYS = new Set(['gitCleanHead', 'fileSha1', 'plannedNew', 'allowedPaths']);

export class ValidationError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ValidationError';
    this.code = 'INVALID_PARAMS';
  }
}

function expandHome(p) {
  if (p === '~') return homedir();
  if (p.startsWith('~/')) return path.join(homedir(), p.slice(2));
  return p;
}

/**
 * Validates that cwd exists, is a directory, and returns its canonical realpath.
 */
export function resolveWorkdir(cwd) {
  if (typeof cwd !== 'string' || !cwd.trim()) {
    throw new ValidationError('cwd must be a non-empty string');
  }
  const expanded = expandHome(cwd);
  let resolved;
  try {
    resolved = realpathSync(expanded);
  } catch (err) {
    throw new ValidationError(`Invalid cwd ${JSON.stringify(cwd)}: ${err.message}`);
  }
  const st = statSync(resolved);
  if (!st.isDirectory()) {
    throw new ValidationError(`cwd is not a directory: ${resolved}`);
  }
  return resolved;
}

function runGit(workdir, args, { encoding = 'utf8' } = {}) {
  return execFileSync('git', ['-C', workdir, ...args], {
    env: { ...process.env, GIT_OPTIONAL_LOCKS: '0' },
    stdio: ['ignore', 'pipe', 'pipe'],
    encoding,
    maxBuffer: 16 * 1024 * 1024,
  });
}

export function detectGitWorktree(workdir) {
  try {
    const inside = runGit(workdir, ['rev-parse', '--is-inside-work-tree']).trim();
    if (inside !== 'true') return { inGit: false, toplevel: null };
    const top = runGit(workdir, ['rev-parse', '--show-toplevel']).trim();
    return { inGit: true, toplevel: realpathSync(top) };
  } catch {
    return { inGit: false, toplevel: null };
  }
}

function validateSafeRelpath(relpath, fieldName, { allowDot = false } = {}) {
  if (typeof relpath !== 'string' || !relpath) {
    throw new ValidationError(`${fieldName} path must be a nonempty string`);
  }
  if (path.posix.isAbsolute(relpath) || path.isAbsolute(relpath) || relpath.includes('\\')) {
    throw new ValidationError(`${fieldName} path must be relative POSIX path: ${relpath}`);
  }
  if (relpath.split('/').includes('..')) {
    throw new ValidationError(`${fieldName} path cannot contain '..': ${relpath}`);
  }
  const normalized = path.posix.normalize(relpath);
  const parts = normalized.split('/').filter(Boolean);
  if (normalized === '.' || parts.length === 0) {
    if (allowDot) return '.';
    throw new ValidationError(`${fieldName} path cannot be '.': ${relpath}`);
  }
  return parts.join('/');
}

function isWithinDir(baseDir, targetPath) {
  const rel = path.relative(baseDir, targetPath);
  return rel === '' || (rel !== '..' && !rel.startsWith(`..${path.sep}`) && !path.isAbsolute(rel));
}

function resolvePathAllowMissing(targetPath, depth = 0) {
  if (depth > 40) {
    const err = new Error(`Too many levels of symbolic links: ${targetPath}`);
    err.code = 'ELOOP';
    throw err;
  }
  try {
    return realpathSync(targetPath);
  } catch (err) {
    if (err.code !== 'ENOENT') throw err;
  }

  const parent = path.dirname(targetPath);
  try {
    const lst = lstatSync(targetPath);
    if (lst.isSymbolicLink()) {
      const linkTarget = readlinkSync(targetPath);
      return resolvePathAllowMissing(path.resolve(parent, linkTarget), depth + 1);
    }
  } catch (err) {
    if (err.code !== 'ENOENT') throw err;
  }

  if (parent === targetPath) return targetPath;
  return path.join(resolvePathAllowMissing(parent, depth), path.basename(targetPath));
}

function ensureWithinWorkdir(workdir, relpath, fieldName) {
  const workdirResolved = realpathSync(workdir);
  const parts = relpath.split('/');
  let current = workdirResolved;

  for (const part of parts.slice(0, -1)) {
    current = path.join(current, part);
    try {
      const lst = lstatSync(current);
      if (lst.isSymbolicLink()) {
        const resolvedParent = resolvePathAllowMissing(current);
        if (!isWithinDir(workdirResolved, resolvedParent)) {
          throw new ValidationError(`${fieldName} parent symlink escapes workdir: ${relpath}`);
        }
        current = resolvedParent;
      }
    } catch (err) {
      if (err.code === 'ENOENT') continue;
      throw err;
    }
  }

  const target = path.join(current, parts[parts.length - 1]);
  const parentDir = path.dirname(target);
  const resolvedParent = resolvePathAllowMissing(parentDir);
  if (!isWithinDir(workdirResolved, resolvedParent)) {
    throw new ValidationError(`${fieldName} escapes workdir: ${relpath}`);
  }
  return target;
}

function computeSha1(filePath) {
  const hash = createHash('sha1');
  const fd = openSync(filePath, 'r');
  const buf = Buffer.allocUnsafe(65536);
  try {
    while (true) {
      const bytesRead = readSync(fd, buf, 0, buf.length, null);
      if (bytesRead === 0) break;
      hash.update(buf.subarray(0, bytesRead));
    }
  } finally {
    closeSync(fd);
  }
  return hash.digest('hex');
}

/**
 * Validates the 4-key baseline contract (gitCleanHead, fileSha1, plannedNew, allowedPaths).
 * Returns normalized allowedPaths array or null.
 */
export function verifyBaseline(baseline, workdir, inGit) {
  if (baseline === undefined || baseline === null) return null;
  if (typeof baseline !== 'object' || Array.isArray(baseline)) {
    throw new ValidationError('baseline must be a JSON object');
  }
  const unknownKeys = Object.keys(baseline).filter((k) => !ALLOWED_BASELINE_KEYS.has(k));
  if (unknownKeys.length > 0) {
    throw new ValidationError(`unknown baseline keys: ${unknownKeys.sort().join(', ')}`);
  }

  if ('gitCleanHead' in baseline) {
    const expectedHead = baseline.gitCleanHead;
    if (typeof expectedHead !== 'string' || !expectedHead) {
      throw new ValidationError('baseline.gitCleanHead must be a nonempty string');
    }
    if (!inGit) {
      throw new ValidationError('baseline.gitCleanHead requires a Git worktree');
    }
    let actualHead;
    try {
      actualHead = runGit(workdir, ['rev-parse', 'HEAD']).trim();
    } catch (err) {
      throw new ValidationError(`baseline.gitCleanHead failed to read HEAD: ${err.message}`);
    }
    if (actualHead !== expectedHead) {
      throw new ValidationError(`baseline.gitCleanHead mismatch: expected ${expectedHead}, got ${actualHead}`);
    }
    const statusOut = runGit(workdir, ['status', '--porcelain']);
    if (statusOut !== '') {
      throw new ValidationError('baseline.gitCleanHead failed: Git worktree is not clean');
    }
  }

  if ('fileSha1' in baseline) {
    const fileSha1 = baseline.fileSha1;
    if (!fileSha1 || typeof fileSha1 !== 'object' || Array.isArray(fileSha1)) {
      throw new ValidationError('baseline.fileSha1 must be an object');
    }
    for (const [rawPath, expectedSha1] of Object.entries(fileSha1)) {
      const relpath = validateSafeRelpath(rawPath, 'baseline.fileSha1');
      if (typeof expectedSha1 !== 'string' || !expectedSha1) {
        throw new ValidationError(`baseline.fileSha1[${relpath}] must be a nonempty string`);
      }
      const target = ensureWithinWorkdir(workdir, relpath, 'baseline.fileSha1');
      let lst;
      try {
        lst = lstatSync(target);
      } catch (err) {
        throw new ValidationError(`baseline.fileSha1 cannot stat ${relpath}: ${err.message}`);
      }
      if (lst.isSymbolicLink() || !lst.isFile()) {
        throw new ValidationError(`baseline.fileSha1 missing regular file: ${relpath}`);
      }
      const actualSha1 = computeSha1(target);
      if (actualSha1 !== expectedSha1.toLowerCase()) {
        throw new ValidationError(`baseline.fileSha1 mismatch for ${relpath}: expected ${expectedSha1}, got ${actualSha1}`);
      }
    }
  }

  if ('plannedNew' in baseline) {
    const plannedNew = baseline.plannedNew;
    if (!Array.isArray(plannedNew)) {
      throw new ValidationError('baseline.plannedNew must be an array');
    }
    for (const rawPath of plannedNew) {
      const relpath = validateSafeRelpath(rawPath, 'baseline.plannedNew');
      const target = ensureWithinWorkdir(workdir, relpath, 'baseline.plannedNew');
      let exists = false;
      try {
        lstatSync(target);
        exists = true;
      } catch (err) {
        if (err.code !== 'ENOENT') {
          throw new ValidationError(`baseline.plannedNew cannot check ${relpath}: ${err.message}`);
        }
      }
      if (exists) {
        throw new ValidationError(`baseline.plannedNew already exists: ${relpath}`);
      }
    }
  }

  if ('allowedPaths' in baseline) {
    if (!Array.isArray(baseline.allowedPaths)) {
      throw new ValidationError('baseline.allowedPaths must be an array');
    }
    return baseline.allowedPaths.map((p) => validateSafeRelpath(p, 'baseline.allowedPaths', { allowDot: true }));
  }

  return null;
}

function pathFingerprint(fullPath) {
  let lst;
  try {
    lst = lstatSync(fullPath);
  } catch {
    return 'missing';
  }
  if (lst.isSymbolicLink()) {
    try {
      return `symlink:${readlinkSync(fullPath)}`;
    } catch (err) {
      return `unreadable-symlink:${err.code || ''}`;
    }
  }
  if (lst.isFile()) {
    try {
      return `file:${computeSha1(fullPath)}`;
    } catch (err) {
      return `unreadable:${lst.mode}:${lst.size}:${lst.mtimeMs}:${err.code || ''}`;
    }
  }
  if (lst.isDirectory()) {
    return 'dir';
  }
  return `other:${lst.mode}`;
}

function toPosixRel(fromDir, fullPath) {
  return path.relative(fromDir, fullPath).split(path.sep).join('/');
}

/**
 * Captures a lightweight workspace snapshot (Git status+HEAD or recursive walk).
 */
export function captureSnapshot(workdir, inGit, toplevel) {
  const snapshot = new Map();
  if (inGit) {
    let head = '';
    try {
      head = runGit(workdir, ['rev-parse', 'HEAD']).trim();
    } catch {
      head = '';
    }
    snapshot.set('.git/HEAD', `HEAD:${head}`);

    const raw = runGit(workdir, ['status', '--porcelain=v1', '-uall', '-z'], { encoding: 'buffer' });
    const records = [];
    let start = 0;
    for (let i = 0; i < raw.length; i += 1) {
      if (raw[i] === 0) {
        if (i > start) records.push(raw.subarray(start, i));
        start = i + 1;
      }
    }
    let idx = 0;
    while (idx < records.length) {
      const rec = records[idx];
      if (rec.length < 4) {
        idx += 1;
        continue;
      }
      const status = rec.subarray(0, 2).toString('ascii');
      const relTop = rec.subarray(3).toString('utf8');
      const entries = [relTop];
      if (status[0] === 'R' || status[0] === 'C' || status[1] === 'R' || status[1] === 'C') {
        if (idx + 1 < records.length) entries.push(records[idx + 1].toString('utf8'));
        idx += 2;
      } else {
        idx += 1;
      }
      for (const item of entries) {
        const fullPath = path.join(toplevel || workdir, item);
        const relWorkdir = toPosixRel(workdir, fullPath);
        snapshot.set(relWorkdir, `${status}:${pathFingerprint(fullPath)}`);
      }
    }
    return snapshot;
  }

  const walk = (dir) => {
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch (err) {
      if (err.code === 'ENOENT') return;
      if (err.code === 'EACCES' || err.code === 'EPERM') {
        if (dir !== workdir) {
          snapshot.set(toPosixRel(workdir, dir), `unreadable-dir:${err.code}`);
        }
        return;
      }
      throw err;
    }
    for (const entry of entries) {
      if (entry.name === '.git') continue;
      const fullPath = path.join(dir, entry.name);
      const rel = toPosixRel(workdir, fullPath);
      if (entry.isSymbolicLink()) {
        try {
          snapshot.set(rel, `symlink:${readlinkSync(fullPath)}`);
        } catch (err) {
          if (err.code === 'ENOENT') continue;
          if (err.code === 'EACCES' || err.code === 'EPERM') {
            snapshot.set(rel, `unreadable-symlink:${err.code}`);
            continue;
          }
          throw err;
        }
      } else if (entry.isDirectory()) {
        walk(fullPath);
      } else {
        snapshot.set(rel, pathFingerprint(fullPath));
      }
    }
  };
  walk(workdir);
  return snapshot;
}

export function diffSnapshots(pre, post, allowedPaths) {
  const keys = new Set([...pre.keys(), ...post.keys()]);
  const changedFiles = [];
  for (const key of keys) {
    if (pre.get(key) !== post.get(key)) {
      changedFiles.push(key);
    }
  }
  changedFiles.sort();

  if (!allowedPaths) {
    return { changed_files: changedFiles, out_of_scope_files: [] };
  }

  const outOfScope = [];
  for (const rel of changedFiles) {
    if (path.posix.isAbsolute(rel) || rel.split('/').includes('..')) {
      outOfScope.push(rel);
      continue;
    }
    if (rel === '.git/HEAD' && !allowedPaths.includes('.git/HEAD') && !allowedPaths.includes('.git')) {
      outOfScope.push(rel);
      continue;
    }
    const allowedMatch = allowedPaths.some(
      (allowed) => allowed === '.' || rel === allowed || rel.startsWith(`${allowed}/`)
    );
    if (!allowedMatch) outOfScope.push(rel);
  }
  outOfScope.sort();
  return { changed_files: changedFiles, out_of_scope_files: outOfScope };
}

export function collectDiffStat(workdir, inGit, maxBytes = 16384) {
  if (!inGit) return '';
  try {
    const out = runGit(workdir, ['diff', 'HEAD', '--stat', '--no-ext-diff', '--no-textconv']);
    return truncateUtf8(out.trimEnd(), maxBytes);
  } catch {
    return '';
  }
}
