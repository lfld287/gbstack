"""Deterministic CLI preflight and runner for GBStack schemaVersion 2 roles."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
import time

SCRIPT_LIB_DIR = Path(__file__).resolve().parent
if str(SCRIPT_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_LIB_DIR))

import resolve_roles  # noqa: E402


RESULT_MARKER = "--- GBSTACK_CLI_RESULT_JSON ---"
DEFAULT_MAX_OUTPUT_BYTES = 65536


class SignalReceived(BaseException):
    def __init__(self, signum):
        super().__init__(f"received signal {signum}")
        self.signum = signum


def add_resolver_arguments(parser, cwd_required=False):
    parser.add_argument(
        "--cwd",
        required=cwd_required,
        default=None if cwd_required else os.getcwd(),
        metavar="DIR",
    )
    parser.add_argument("--user-config-dir", metavar="DIR")
    parser.add_argument("--no-user-config", action="store_true")
    parser.add_argument("--project-config-dir", metavar="DIR")
    parser.add_argument("--no-project-config", action="store_true")
    parser.add_argument("--default-config", metavar="FILE")
    parser.add_argument("--allow-project-cli", action="store_true")


def resolve_workdir(cwd_value):
    try:
        workdir = Path(cwd_value).expanduser().resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Error: invalid --cwd {cwd_value!r}: {error}", file=sys.stderr)
        return None
    if not workdir.is_dir():
        print(f"Error: --cwd is not a directory: {workdir}", file=sys.stderr)
        return None
    return workdir


def resolve_binary(cli_cfg, role_id):
    candidates = [cli_cfg["binary"], *cli_cfg.get("fallbackBinaries", [])]
    for candidate in candidates:
        if "/" in candidate or candidate.startswith("~"):
            try:
                path = Path(candidate).expanduser()
            except (ValueError, RuntimeError):
                continue
            if path.is_file() and os.access(path, os.X_OK):
                return os.path.abspath(str(path))
        else:
            found = shutil.which(candidate)
            if found and os.path.isfile(found) and os.access(found, os.X_OK):
                return os.path.abspath(found)
    print(
        f"Error: no executable binary found for role {role_id!r} (tried: {', '.join(candidates)})",
        file=sys.stderr,
    )
    return None


def substitute_placeholders(text, *, binary, workdir, extra_dir=None):
    result = text.replace("{binary}", binary).replace("{workdir}", workdir)
    if extra_dir is not None:
        result = result.replace("{dir}", extra_dir)
    return result


def _pgid_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_process_group(proc):
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        proc.poll()
        return
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        proc.poll()
        if proc.returncode is not None and not _pgid_alive(pgid):
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def _run_process_group(cmd, *, cwd, env, stdin_mode, stdin_bytes, timeout, merge_stderr=False):
    def _signal_handler(signum, _frame):
        raise SignalReceived(signum)

    prev_sigterm = signal.getsignal(signal.SIGTERM)
    prev_sigint = signal.getsignal(signal.SIGINT)
    started = time.monotonic()
    timed_out = False
    proc = None
    stdout_bytes = b""
    stderr_bytes = b""
    exit_code = None
    signal_exit = None
    os_error = None

    try:
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdin=stdin_mode,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout_bytes, stderr_bytes = proc.communicate(input=stdin_bytes, timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(proc)
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=2.0)
            except (subprocess.TimeoutExpired, OSError):
                stdout_bytes, stderr_bytes = b"", b""
            exit_code = 124
    except SignalReceived as sig:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if proc is not None:
            _terminate_process_group(proc)
        signal_exit = 128 + sig.signum
    except OSError as error:
        os_error = error
    finally:
        signal.signal(signal.SIGTERM, prev_sigterm)
        signal.signal(signal.SIGINT, prev_sigint)

    duration_ms = int((time.monotonic() - started) * 1000)
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes or b"",
        "signal_exit": signal_exit,
        "os_error": os_error,
    }


def parse_preflight_args(argv):
    parser = argparse.ArgumentParser(prog="preflight-cli.sh", allow_abbrev=False)
    add_resolver_arguments(parser, cwd_required=False)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--role", action="append", metavar="ROLE_ID")
    target.add_argument("--review", metavar="REVIEW_ID")
    target.add_argument("--all", action="store_true")
    return parser.parse_args(argv)


def run_preflight(argv):
    args = parse_preflight_args(argv)
    workdir = resolve_workdir(args.cwd)
    if workdir is None:
        return 2
    try:
        resolver = resolve_roles.Resolver(args)
    except resolve_roles.ConfigError as error:
        print(error, file=sys.stderr)
        return 1

    targets = []
    if args.all:
        targets = [role for role in resolver.roles.values() if role["mode"] == "cli"]
    elif args.review is not None:
        if args.review not in resolver.reviews:
            print(f"review {args.review!r} is missing or disabled", file=sys.stderr)
            return 3
        review = resolver.reviews[args.review]
        targets = [resolver.roles[role_id] for role_id in review["cliRoles"]]
    else:
        for role_id in dict.fromkeys(args.role):
            if role_id not in resolver.roles:
                print(f"role {role_id!r} is missing or disabled", file=sys.stderr)
                return 3
            role = resolver.roles[role_id]
            if role["mode"] != "cli":
                print(f"Error: role {role_id!r} is in {role['mode']!r} mode, not 'cli'", file=sys.stderr)
                return 1
            targets.append(role)

    if not targets:
        print("No active CLI roles to preflight; skipping.")
        return 0

    for role in targets:
        role_id = role["id"]
        cli_cfg = role["cli"]
        binary_path = resolve_binary(cli_cfg, role_id)
        if binary_path is None:
            return 127
        preflight = cli_cfg.get("preflight")
        if preflight is None:
            print(f"Preflight OK: {role_id} ({binary_path}, no preflight command)")
            continue
        cmd = [
            substitute_placeholders(part, binary=binary_path, workdir=str(workdir))
            for part in preflight["command"]
        ]
        env = os.environ.copy()
        env.update(cli_cfg.get("env", {}))
        env.update(preflight.get("env", {}))
        timeout = preflight.get("timeoutSeconds", 30)
        outcome = _run_process_group(
            cmd,
            cwd=workdir,
            env=env,
            stdin_mode=subprocess.DEVNULL,
            stdin_bytes=None,
            timeout=timeout,
            merge_stderr=True,
        )
        if outcome["signal_exit"] is not None:
            return outcome["signal_exit"]
        if outcome["os_error"] is not None:
            print(
                f"Error: preflight command failed to start for role {role_id!r}: {outcome['os_error']}",
                file=sys.stderr,
            )
            return 1

        output = outcome["stdout_bytes"].decode("utf-8", errors="replace")
        if outcome["timed_out"]:
            print(
                f"Error: preflight command timed out after {timeout}s for role {role_id!r}\n{output}".rstrip(),
                file=sys.stderr,
            )
            return 1
        if outcome["exit_code"] != 0:
            print(
                f"Error: preflight command exited {outcome['exit_code']} for role {role_id!r}:\n{output}".rstrip(),
                file=sys.stderr,
            )
            return 1
        expected = preflight.get("outputContains")
        if expected is not None and expected not in output:
            print(
                f"Error: preflight output for role {role_id!r} did not contain {expected!r}:\n{output}".rstrip(),
                file=sys.stderr,
            )
            return 1
        print(f"Preflight OK: {role_id} ({binary_path})")

    return 0


def parse_run_args(argv):
    parser = argparse.ArgumentParser(prog="run-cli.sh", allow_abbrev=False)
    parser.add_argument("--role", required=True, metavar="ROLE_ID")
    add_resolver_arguments(parser, cwd_required=True)
    parser.add_argument("--add-dir", action="append", default=[], metavar="DIR")
    parser.add_argument("--timeout", type=int, metavar="SEC")
    parser.add_argument("--baseline-json", metavar="JSON")
    parser.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES, metavar="N")
    return parser.parse_args(argv)


def _git_command(workdir, *git_args, text=True):
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", "-C", str(workdir), *git_args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=text,
        check=False,
    )


def detect_git_worktree(workdir):
    try:
        result = _git_command(workdir, "rev-parse", "--is-inside-work-tree", text=True)
    except OSError:
        return False, None
    if result.returncode != 0 or result.stdout.strip() != "true":
        return False, None
    top_result = _git_command(workdir, "rev-parse", "--show-toplevel", text=True)
    if top_result.returncode != 0:
        return True, workdir
    return True, Path(top_result.stdout.strip()).resolve()


ALLOWED_BASELINE_KEYS = frozenset({"gitCleanHead", "fileSha1", "plannedNew", "allowedPaths"})


def validate_safe_relpath(relpath, field_name, *, allow_dot=False):
    if type(relpath) is not str or not relpath:
        raise ValueError(f"{field_name} path must be a nonempty string")
    if os.path.isabs(relpath) or "\\" in relpath:
        raise ValueError(f"{field_name} path must be relative POSIX path: {relpath!r}")
    parsed = PurePosixPath(relpath)
    parts = parsed.parts
    if ".." in parts:
        raise ValueError(f"{field_name} path cannot contain '..': {relpath!r}")
    if not parts:
        if allow_dot:
            return "."
        raise ValueError(f"{field_name} path cannot be '.': {relpath!r}")
    return parsed.as_posix()


def ensure_within_workdir(workdir, relpath, field_name):
    workdir_resolved = workdir.resolve()
    parts = PurePosixPath(relpath).parts
    current = workdir_resolved
    for part in parts[:-1]:
        current = current / part
        if os.path.islink(current):
            resolved_parent = current.resolve()
            try:
                resolved_parent.relative_to(workdir_resolved)
            except ValueError as error:
                raise ValueError(f"{field_name} parent symlink escapes workdir: {relpath!r}") from error
            current = resolved_parent
    target = current / parts[-1]
    try:
        target.parent.resolve().relative_to(workdir_resolved)
    except ValueError as error:
        raise ValueError(f"{field_name} escapes workdir: {relpath!r}") from error
    return target


def compute_sha1(path):
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_baseline(baseline_json, workdir, in_git):
    try:
        baseline = json.loads(baseline_json, parse_constant=resolve_roles.reject_constant)
    except (ValueError, RecursionError) as error:
        raise ValueError(f"invalid --baseline-json: {error}") from error
    if not isinstance(baseline, dict):
        raise ValueError("--baseline-json must be a JSON object")
    unknown_keys = sorted(set(baseline) - ALLOWED_BASELINE_KEYS)
    if unknown_keys:
        raise ValueError(f"unknown --baseline-json keys: {', '.join(unknown_keys)}")

    if "gitCleanHead" in baseline:
        git_clean_head = baseline["gitCleanHead"]
        if type(git_clean_head) is not str or not git_clean_head:
            raise ValueError("baseline.gitCleanHead must be a nonempty string")
        if not in_git:
            raise ValueError("baseline.gitCleanHead requires a Git worktree")
        head_res = _git_command(workdir, "rev-parse", "HEAD", text=True)
        if head_res.returncode != 0 or head_res.stdout.strip() != git_clean_head:
            actual = head_res.stdout.strip() if head_res.returncode == 0 else head_res.stderr.strip()
            raise ValueError(f"baseline.gitCleanHead mismatch: expected {git_clean_head}, got {actual!r}")
        status_res = _git_command(workdir, "status", "--porcelain", text=True)
        if status_res.returncode != 0 or status_res.stdout != "":
            raise ValueError("baseline.gitCleanHead failed: Git worktree is not clean")

    if "fileSha1" in baseline:
        file_sha1 = baseline["fileSha1"]
        if not isinstance(file_sha1, dict):
            raise ValueError("baseline.fileSha1 must be an object")
        for raw_path, expected_sha1 in file_sha1.items():
            relpath = validate_safe_relpath(raw_path, "baseline.fileSha1")
            if type(expected_sha1) is not str or not expected_sha1:
                raise ValueError(f"baseline.fileSha1[{relpath!r}] must be a nonempty string")
            try:
                target = ensure_within_workdir(workdir, relpath, "baseline.fileSha1")
                if os.path.islink(target) or not target.is_file():
                    raise ValueError(f"baseline.fileSha1 missing regular file: {relpath}")
                actual_sha1 = compute_sha1(target)
            except OSError as error:
                raise ValueError(f"baseline.fileSha1 cannot read {relpath}: {error}") from error
            if actual_sha1 != expected_sha1.lower():
                raise ValueError(
                    f"baseline.fileSha1 mismatch for {relpath}: expected {expected_sha1}, got {actual_sha1}"
                )

    if "plannedNew" in baseline:
        planned_new = baseline["plannedNew"]
        if not isinstance(planned_new, list):
            raise ValueError("baseline.plannedNew must be a list")
        for raw_path in planned_new:
            relpath = validate_safe_relpath(raw_path, "baseline.plannedNew")
            try:
                target = ensure_within_workdir(workdir, relpath, "baseline.plannedNew")
                exists = os.path.lexists(target)
            except OSError as error:
                raise ValueError(f"baseline.plannedNew cannot check {relpath}: {error}") from error
            if exists:
                raise ValueError(f"baseline.plannedNew already exists: {relpath}")

    allowed_paths = None
    if "allowedPaths" in baseline:
        if not isinstance(baseline["allowedPaths"], list):
            raise ValueError("baseline.allowedPaths must be a list")
        allowed_paths = [
            validate_safe_relpath(raw_path, "baseline.allowedPaths", allow_dot=True)
            for raw_path in baseline["allowedPaths"]
        ]

    return allowed_paths


def _path_fingerprint(full_path):
    if os.path.islink(full_path):
        return ("symlink", os.readlink(full_path))
    if os.path.isfile(full_path):
        try:
            return ("file", compute_sha1(full_path))
        except OSError as error:
            st = os.lstat(full_path)
            return ("unreadable", st.st_mode, st.st_size, st.st_mtime_ns, error.errno)
    if os.path.isdir(full_path):
        return ("dir", None)
    return ("missing", None)


def capture_snapshot(workdir, in_git, toplevel):
    snapshot = {}
    if in_git:
        head_res = _git_command(workdir, "rev-parse", "HEAD", text=True)
        snapshot[".git/HEAD"] = ("HEAD", head_res.stdout.strip() if head_res.returncode == 0 else None)
        res = _git_command(workdir, "status", "--porcelain=v1", "-uall", "-z", text=False)
        if res.returncode != 0:
            raise RuntimeError(f"git status failed: {res.stderr.decode('utf-8', errors='replace')}")
        records = res.stdout.split(b"\0")
        if records and records[-1] == b"":
            records.pop()
        idx = 0
        while idx < len(records):
            rec = records[idx]
            if len(rec) < 4:
                idx += 1
                continue
            status = rec[:2].decode("ascii", errors="replace")
            rel_top = rec[3:].decode("utf-8", errors="replace")
            entries = [rel_top]
            if status[0] in ("R", "C") or status[1] in ("R", "C"):
                if idx + 1 < len(records):
                    entries.append(records[idx + 1].decode("utf-8", errors="replace"))
                idx += 2
            else:
                idx += 1
            for item in entries:
                full_path = toplevel / item
                rel_workdir = PurePosixPath(Path(os.path.relpath(full_path, workdir))).as_posix()
                snapshot[rel_workdir] = (status, _path_fingerprint(full_path))
        return snapshot

    for dirpath, dirnames, filenames in os.walk(workdir, followlinks=False):
        kept_dirs = []
        for dirname in dirnames:
            if dirname == ".git":
                continue
            full_dir = os.path.join(dirpath, dirname)
            if os.path.islink(full_dir):
                rel = PurePosixPath(Path(os.path.relpath(full_dir, workdir))).as_posix()
                snapshot[rel] = ("symlink", os.readlink(full_dir))
            else:
                kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in filenames:
            full_file = os.path.join(dirpath, filename)
            rel = PurePosixPath(Path(os.path.relpath(full_file, workdir))).as_posix()
            snapshot[rel] = _path_fingerprint(full_file)
    return snapshot


def compute_out_of_scope(changed_files, allowed_paths):
    if allowed_paths is None:
        return []
    out_of_scope = []
    for rel in changed_files:
        if os.path.isabs(rel) or ".." in PurePosixPath(rel).parts:
            out_of_scope.append(rel)
            continue
        if rel == ".git/HEAD" and ".git/HEAD" not in allowed_paths and ".git" not in allowed_paths:
            out_of_scope.append(rel)
            continue
        if any(allowed == "." or rel == allowed or rel.startswith(allowed + "/") for allowed in allowed_paths):
            continue
        out_of_scope.append(rel)
    return sorted(out_of_scope)


def truncate_output(text, max_bytes):
    if max_bytes <= 0:
        return ""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text
    omitted = len(raw) - max_bytes
    marker = f"\n... [truncated {omitted} bytes] ...\n".encode("utf-8")
    if max_bytes <= len(marker):
        return raw[:max_bytes].decode("utf-8", errors="ignore")
    budget = max_bytes - len(marker)
    head_budget = budget // 2
    tail_budget = budget - head_budget
    head = raw[:head_budget].decode("utf-8", errors="ignore")
    tail = raw[len(raw) - tail_budget:].decode("utf-8", errors="ignore") if tail_budget else ""
    return head + marker.decode("utf-8") + tail


def run_cli_command(argv):
    args = parse_run_args(argv)
    if args.timeout is not None and args.timeout < 1:
        print("Error: --timeout must be >= 1", file=sys.stderr)
        return 2
    if args.max_output_bytes < 0:
        print("Error: --max-output-bytes must be >= 0", file=sys.stderr)
        return 2

    workdir = resolve_workdir(args.cwd)
    if workdir is None:
        return 2

    try:
        resolver = resolve_roles.Resolver(args)
    except resolve_roles.ConfigError as error:
        print(error, file=sys.stderr)
        return 1

    if args.role not in resolver.roles:
        print(f"role {args.role!r} is missing or disabled", file=sys.stderr)
        return 3
    role = resolver.roles[args.role]
    if role["mode"] != "cli":
        print(f"Error: role {args.role!r} is in {role['mode']!r} mode, not 'cli'", file=sys.stderr)
        return 1

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("Error: prompt on stdin must not be empty", file=sys.stderr)
        return 2

    cli_cfg = role["cli"]
    binary_path = resolve_binary(cli_cfg, role["id"])
    if binary_path is None:
        return 127

    in_git, toplevel = detect_git_worktree(workdir)

    allowed_paths = None
    if args.baseline_json is not None:
        try:
            allowed_paths = verify_baseline(args.baseline_json, workdir, in_git)
        except ValueError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 2

    if args.add_dir and not cli_cfg["extraDirArgs"]:
        print(f"Error: role {role['id']!r} does not configure extraDirArgs for --add-dir", file=sys.stderr)
        return 2

    cmd = [binary_path]
    cmd.extend(
        substitute_placeholders(part, binary=binary_path, workdir=str(workdir))
        for part in cli_cfg["args"]
    )
    if not in_git:
        cmd.extend(
            substitute_placeholders(part, binary=binary_path, workdir=str(workdir))
            for part in cli_cfg["nonGitArgs"]
        )
    for extra_dir in args.add_dir:
        extra_dir_val = str(Path(extra_dir).expanduser()) if extra_dir.startswith("~") else extra_dir
        cmd.extend(
            substitute_placeholders(part, binary=binary_path, workdir=str(workdir), extra_dir=extra_dir_val)
            for part in cli_cfg["extraDirArgs"]
        )

    if cli_cfg["promptInput"] == "stdin":
        if cli_cfg.get("stdinArg") is not None:
            cmd.append(cli_cfg["stdinArg"])
        stdin_mode = subprocess.PIPE
        stdin_bytes = prompt.encode("utf-8")
    else:
        cmd.extend(cli_cfg["promptPrefixArgs"])
        cmd.append(prompt)
        stdin_mode = subprocess.DEVNULL
        stdin_bytes = None

    pre_snapshot = capture_snapshot(workdir, in_git, toplevel)

    env = os.environ.copy()
    env.update(cli_cfg["env"])
    timeout = args.timeout if args.timeout is not None else cli_cfg["timeoutSeconds"]

    outcome = _run_process_group(
        cmd,
        cwd=workdir,
        env=env,
        stdin_mode=stdin_mode,
        stdin_bytes=stdin_bytes,
        timeout=timeout,
        merge_stderr=False,
    )
    if outcome["signal_exit"] is not None:
        return outcome["signal_exit"]
    if outcome["os_error"] is not None:
        print(f"Error: failed to execute CLI binary {binary_path!r}: {outcome['os_error']}", file=sys.stderr)
        return 1

    exit_code = outcome["exit_code"]
    timed_out = outcome["timed_out"]
    duration_ms = outcome["duration_ms"]
    stdout_bytes = outcome["stdout_bytes"]
    stderr_bytes = outcome["stderr_bytes"]
    post_snapshot = capture_snapshot(workdir, in_git, toplevel)
    changed_files = sorted(
        key for key in (set(pre_snapshot) | set(post_snapshot))
        if pre_snapshot.get(key) != post_snapshot.get(key)
    )
    out_of_scope_files = compute_out_of_scope(changed_files, allowed_paths)

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    if stderr_text:
        sys.stderr.write(stderr_text)
        if not stderr_text.endswith("\n"):
            sys.stderr.write("\n")

    readonly_violation = bool(role["readonly"] and changed_files)
    empty_output = stdout_text.strip() == ""

    rendered_stdout = truncate_output(stdout_text, args.max_output_bytes)
    if rendered_stdout:
        sys.stdout.write(rendered_stdout)
        if not rendered_stdout.endswith("\n"):
            sys.stdout.write("\n")

    result_payload = {
        "role": role["id"],
        "binary": binary_path,
        "exitCode": exit_code,
        "timedOut": timed_out,
        "durationMs": duration_ms,
        "emptyOutput": empty_output,
        "readonlyViolation": readonly_violation,
        "workdir": str(workdir),
        "changedFiles": changed_files,
        "outOfScopeFiles": out_of_scope_files,
    }
    sys.stdout.write(f"{RESULT_MARKER}\n{json.dumps(result_payload, separators=(',', ':'))}\n")

    if timed_out:
        return 124
    if readonly_violation:
        return 4
    if role["readonly"] and exit_code == 0 and empty_output:
        return 5
    if exit_code != 0:
        return 1
    return 0


def main(argv=None):
    args_list = sys.argv[1:] if argv is None else list(argv)
    if not args_list or args_list[0] not in ("preflight", "run"):
        print("Usage: run_cli.py {preflight|run} ...", file=sys.stderr)
        return 2
    subcommand = args_list[0]
    rest = args_list[1:]
    if subcommand == "preflight":
        return run_preflight(rest)
    return run_cli_command(rest)


if __name__ == "__main__":
    sys.exit(main())
