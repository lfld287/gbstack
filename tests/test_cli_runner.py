import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts" / "preflight-cli.sh"
RUNNER = ROOT / "scripts" / "run-cli.sh"
MARKER = "--- GBSTACK_CLI_RESULT_JSON ---"


class CliRunnerTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PREFLIGHT.is_file(), "scripts/preflight-cli.sh is missing")
        self.assertTrue(RUNNER.is_file(), "scripts/run-cli.sh is missing")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base / "home"
        self.bin = self.base / "bin"
        self.user_dir = self.home / ".gemini" / "config" / "gbstack"
        self.workdir = self.base / "workdir"
        self.project_dir = self.workdir / ".agents" / "gbstack"
        for directory in (self.home, self.bin, self.workdir):
            directory.mkdir(parents=True)
        for tool in ("sh", "bash", "cat", "dirname", "pwd", "printf", "readlink", "rm", "sleep", "touch", "git"):
            resolved = shutil.which(tool)
            if resolved:
                (self.bin / tool).symlink_to(resolved)
        (self.bin / "python3").symlink_to(sys.executable)
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "CODEX_HOME": str(self.base / "codex_home"),
            "GBSTACK_USER_CONFIG_DIR": str(self.user_dir),
            "GBSTACK_PROJECT_CONFIG_DIR": str(self.project_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def write(self, path, contents):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def make_executable(self, path, script_body):
        self.write(path, script_body)
        path.chmod(0o755)
        return path

    def write_project_config(self, config):
        return self.write(self.project_dir / "roles.json", json.dumps(config))

    def write_user_config(self, config):
        return self.write(self.user_dir / "roles.json", json.dumps(config))

    def run_preflight(self, *args, env=None, cwd=None, executable=PREFLIGHT):
        return subprocess.run(
            [str(executable), *map(str, args)],
            cwd=self.workdir if cwd is None else cwd,
            env=self.env if env is None else env,
            capture_output=True,
            text=True,
            check=False,
        )

    def run_runner(self, *args, stdin_text="hello prompt\n", env=None, cwd=None, executable=RUNNER):
        return subprocess.run(
            [str(executable), *map(str, args)],
            input=stdin_text,
            cwd=self.workdir if cwd is None else cwd,
            env=self.env if env is None else env,
            capture_output=True,
            text=True,
            check=False,
        )

    def parse_runner_output(self, stdout):
        parts = stdout.rsplit(f"{MARKER}\n", 1)
        self.assertEqual(len(parts), 2, f"missing result marker in stdout: {stdout!r}")
        body, trailer = parts
        json_lines = trailer.strip().splitlines()
        self.assertEqual(len(json_lines), 1, f"expected single compact JSON line, got: {trailer!r}")
        return body, json.loads(json_lines[0])

    def init_git_repo(self, path=None):
        repo = self.workdir if path is None else path
        git = shutil.which("git")
        for cmd in (
            [git, "-C", str(repo), "init"],
            [git, "-C", str(repo), "config", "user.email", "test@example.com"],
            [git, "-C", str(repo), "config", "user.name", "Test"],
        ):
            subprocess.run(cmd, check=True, capture_output=True)
        return repo

    def commit_file(self, relpath, content, repo=None):
        target_repo = self.workdir if repo is None else repo
        file_path = target_repo / relpath
        self.write(file_path, content)
        git = shutil.which("git")
        subprocess.run([git, "-C", str(target_repo), "add", "--", relpath], check=True, capture_output=True)
        subprocess.run([git, "-C", str(target_repo), "commit", "-m", f"add {relpath}"], check=True, capture_output=True)
        head = subprocess.run([git, "-C", str(target_repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
        return head.stdout.strip()

    def test_preflight_binary_lookup_fallback_and_missing_127(self):
        # Missing on PATH and fallback -> 127
        result = self.run_preflight("--role", "worker")
        self.assertEqual(result.returncode, 127, (result.stdout, result.stderr))
        self.assertIn("no executable binary found", result.stderr)

        # Fallback binary under ~/.local/bin/codex works
        fallback = self.make_executable(
            self.home / ".local" / "bin" / "codex",
            "#!/bin/bash\nprintf 'Logged in via fallback\\n'\n",
        )
        ok = self.run_preflight("--role", "worker")
        self.assertEqual(ok.returncode, 0, (ok.stdout, ok.stderr))
        self.assertIn(str(fallback), ok.stdout)

        # PATH binary takes priority over fallback
        primary = self.make_executable(
            self.bin / "codex",
            "#!/bin/bash\nprintf 'Logged in via primary\\n'\n",
        )
        ok_primary = self.run_preflight("--role", "worker")
        self.assertEqual(ok_primary.returncode, 0, (ok_primary.stdout, ok_primary.stderr))
        self.assertIn(str(primary), ok_primary.stdout)

    def test_preflight_output_contains_failure_and_env_precedence(self):
        self.make_executable(
            self.bin / "mycli",
            "#!/bin/bash\n"
            "printf 'OUT_TOKEN=%s CLI_ENV=%s\\n' \"$TOKEN\" \"$CLI_ONLY\"\n"
            "exit \"${PREFLIGHT_EXIT:-0}\"\n",
        )
        self.write_user_config({
            "roles": {
                "custom-cli": {
                    "readonly": True,
                    "workspace": "inherit",
                    "prompt": None,
                    "mode": "cli",
                    "cli": {
                        "provider": "custom",
                        "binary": "mycli",
                        "promptInput": "arg",
                        "outerModel": "gemini-3.8-flash-medium",
                        "env": {"TOKEN": "from-cli", "CLI_ONLY": "yes"},
                        "preflight": {
                            "command": ["{binary}"],
                            "outputContains": "OUT_TOKEN=from-preflight",
                            "env": {"TOKEN": "from-preflight"},
                        },
                    },
                },
            },
        })
        ok = self.run_preflight("--role", "custom-cli")
        self.assertEqual(ok.returncode, 0, (ok.stdout, ok.stderr))

        # outputContains mismatch -> 1 with full output
        self.write_user_config({
            "roles": {
                "custom-cli": {
                    "readonly": True,
                    "workspace": "inherit",
                    "prompt": None,
                    "mode": "cli",
                    "cli": {
                        "provider": "custom",
                        "binary": "mycli",
                        "promptInput": "arg",
                        "outerModel": "gemini-3.8-flash-medium",
                        "preflight": {
                            "command": ["{binary}"],
                            "outputContains": "MISSING_MARKER",
                            "env": {"TOKEN": "mismatch", "CLI_ONLY": "no"},
                        },
                    },
                },
            },
        })
        mismatch = self.run_preflight("--role", "custom-cli")
        self.assertEqual(mismatch.returncode, 1)
        self.assertIn("OUT_TOKEN=mismatch CLI_ENV=no", mismatch.stderr)

        # Non-zero exit -> 1 with full output
        fail_exit = self.run_preflight("--role", "custom-cli", env=dict(self.env, PREFLIGHT_EXIT="7"))
        self.assertEqual(fail_exit.returncode, 1)
        self.assertIn("exited 7", fail_exit.stderr)
        self.assertIn("OUT_TOKEN=mismatch", fail_exit.stderr)

    def test_preflight_targets_all_review_and_native_skip(self):
        # Native-only review skips cleanly with 0 even without codex binary
        native_review = self.run_preflight("--review", "default")
        self.assertEqual(native_review.returncode, 0, native_review.stderr)
        self.assertIn("skipping", native_review.stdout.lower())

        # --all with all CLI roles switched to native skips cleanly
        self.write_project_config({
            "roles": {
                "worker": {"mode": "native"},
                "reviewer-codex": {"mode": "native", "native": {"provider": "test", "model": "m"}},
            },
        })
        all_native = self.run_preflight("--all")
        self.assertEqual(all_native.returncode, 0, all_native.stderr)

        # Missing role / review -> 3; native role explicitly -> 1
        self.assertEqual(self.run_preflight("--role", "missing").returncode, 3)
        self.assertEqual(self.run_preflight("--review", "missing").returncode, 3)
        self.assertEqual(self.run_preflight("--role", "scout").returncode, 1)

        # --review council checks reviewer-codex
        self.project_dir.joinpath("roles.json").unlink()
        self.make_executable(self.bin / "codex", "#!/bin/bash\nprintf 'Logged in\\n'\n")
        council = self.run_preflight("--review", "council")
        self.assertEqual(council.returncode, 0, council.stderr)
        self.assertIn("reviewer-codex", council.stdout)

    def test_run_profiles_default_rework_and_fallback(self):
        stub = (
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read(), "
            "'env': [os.getenv('BASE_TOKEN'), os.getenv('TOKEN')]}))\n"
        )
        primary = self.make_executable(self.bin / "primary", stub)
        secondary = self.make_executable(self.bin / "secondary", stub)
        cli = {
            "binary": str(primary), "provider": "base-provider", "args": ["base"],
            "env": {"BASE_TOKEN": "present", "TOKEN": "base"},
            "rework": {"args": ["again"], "env": {"TOKEN": "again"}},
            "fallback": {"args": ["backup"], "env": {"TOKEN": "backup"}},
        }
        options = ("--role", "worker", "--cwd", self.workdir)
        stdin_args = ["--skip-git-repo-check", "-"]
        for requested, override, effective, path, provider, argv, stdin, env in [
            ("default", cli, "default", primary, "base-provider", ["base", *stdin_args], "task", ["present", "base"]),
            ("rework", cli, "rework", primary, "base-provider", ["again", *stdin_args], "task", ["present", "again"]),
            ("rework", dict(cli, rework=None), "default", primary, "base-provider",
             ["base", *stdin_args], "task", ["present", "base"]),
            ("fallback", cli, "fallback", primary, "base-provider",
             ["backup", *stdin_args], "task", ["present", "backup"]),
            ("fallback", dict(cli, fallback={
                "binary": str(secondary), "provider": "backup-provider", "promptInput": "arg",
                "promptPrefixArgs": ["-p"], "env": {"TOKEN": "cross-tool"},
            }), "fallback", secondary, "backup-provider", ["-p", "task"], "", [None, "cross-tool"]),
        ]:
            with self.subTest(requested=requested, effective=effective, path=path):
                self.write_user_config({"roles": {"worker": {"cli": override}}})
                result = self.run_runner(*options, "--profile", requested, stdin_text="task")
                self.assertEqual(result.returncode, 0, result.stderr)
                body, payload = self.parse_runner_output(result.stdout)
                self.assertEqual(json.loads(body), {"argv": argv, "stdin": stdin, "env": env})
                self.assertEqual(payload["binary"], str(path))
                self.assertEqual((payload["profile"], payload["effectiveProfile"], payload["provider"]),
                                 (requested, effective, provider))

        self.write_user_config({"roles": {"worker": {"cli": dict(cli, fallback=None)}}})
        for profile, message in [("fallback", "does not configure cli.fallback"), ("all", "invalid choice")]:
            with self.subTest(profile=profile):
                # Keep stdin open: rejection must happen before consuming the prompt or checking the baseline.
                with subprocess.Popen(
                    [str(RUNNER), *map(str, options), "--profile", profile, "--baseline-json", "not-json"],
                    cwd=self.workdir, env=self.env, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ) as proc:
                    try:
                        self.assertEqual(proc.wait(timeout=5), 2)
                        self.assertEqual(proc.stdout.read(), "")
                        self.assertIn(message, proc.stderr.read())
                    finally:
                        if proc.poll() is None:
                            proc.kill()

    def test_run_never_auto_falls_back_on_failure_or_partial_edits(self):
        fallback_calls = self.base / "fallback-calls"
        fallback = self.make_executable(self.bin / "backup", (
            f"#!/bin/bash\nprintf 'called\\n' >> {shlex.quote(str(fallback_calls))}\n"
        ))
        primary = self.make_executable(self.bin / "primary", (
            "#!/bin/bash\n"
            "case \"$ACTION\" in\n"
            "  timeout) exec sleep 20 ;;\n"
            "  partial) printf 'partial' > partial.txt ;;\n"
            "esac\n"
            "exit 1\n"
        ))
        self.write_user_config({"roles": {"worker": {"cli": {
            "binary": str(primary), "args": [], "timeoutSeconds": 1,
            "fallback": {"binary": str(fallback)},
        }}}})
        for action, status, changed in [("fail", 1, []), ("timeout", 124, []), ("partial", 1, ["partial.txt"])]:
            with self.subTest(action=action):
                result = self.run_runner("--role", "worker", "--cwd", self.workdir, env=dict(self.env, ACTION=action))
                self.assertEqual(result.returncode, status, result.stderr)
                _, payload = self.parse_runner_output(result.stdout)
                self.assertEqual(payload["exitCode"], status)
                self.assertEqual(payload["changedFiles"], changed)
                self.assertEqual((payload["profile"], payload["effectiveProfile"], payload["provider"]),
                                 ("default", "default", "openai"))
                self.assertFalse(fallback_calls.exists())

    def test_preflight_profiles_default_rework_fallback_and_all(self):
        calls = self.base / "preflight-calls"
        stub = (
            "#!/bin/bash\n"
            "value=\"${0##*/}:$1:$TOKEN\"\n"
            f"printf '%s\\n' \"$value\" >> {shlex.quote(str(calls))}\n"
            "printf '%s\\n' \"$value\"\n"
        )
        self.make_executable(self.bin / "primary", stub)
        fallback = {"binary": "backup", "env": {"TOKEN": "fallback-cli"}, "preflight": {
            "command": ["{binary}", "fallback"], "env": {"TOKEN": "fallback-pf"},
            "outputContains": "backup:fallback:fallback-pf",
        }}
        self.write_user_config({"roles": {"reviewer-codex": {"cli": {
            "binary": "primary", "env": {"TOKEN": "base"},
            "preflight": {"command": ["{binary}", "default"], "outputContains": "primary:default:base"},
            "rework": {"env": {"TOKEN": "again"}, "preflight": {"command": ["{binary}", "rework"]}},
            "fallback": fallback,
        }}}})
        role = ("--role", "reviewer-codex")
        missing = self.run_preflight(*role, "--profile", "fallback")
        self.assertEqual(missing.returncode, 127, missing.stderr)
        self.assertIn("backup", missing.stderr)
        for profile, expected in [("default", "primary:default:base"), ("rework", "primary:rework:again")]:
            with self.subTest(profile=profile):
                result = self.run_preflight(*role, "--profile", profile)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls.read_text().splitlines()[-1], expected)
        unconfigured = self.run_preflight("--role", "worker", "--profile", "fallback")
        self.assertEqual(unconfigured.returncode, 2, unconfigured.stderr)
        self.assertIn("does not configure cli.fallback", unconfigured.stderr)
        native = self.run_preflight("--role", "scout", "--profile", "fallback")
        self.assertEqual(native.returncode, 1, native.stderr)
        self.assertIn("'native' mode", native.stderr)

        self.make_executable(self.bin / "backup", stub)
        for target in (role, ("--review", "council"), ("--all",)):
            with self.subTest(target=target):
                calls.unlink(missing_ok=True)
                result = self.run_preflight(*target, "--profile", "fallback")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls.read_text().splitlines(), ["backup:fallback:fallback-pf"])
        calls.unlink()
        result = self.run_preflight(*role, "--profile", "all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.read_text().splitlines(), [
            "primary:default:base", "primary:rework:again", "backup:fallback:fallback-pf",
        ])

        self.write_user_config({"roles": {"worker": {"cli": {
            "binary": "primary", "env": {"TOKEN": "worker-base"}, "rework": None,
            "preflight": {"command": ["{binary}", "default"], "outputContains": "primary:default:worker-base"},
        }}}})
        calls.unlink()
        result = self.run_preflight("--role", "worker", "--profile", "rework")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.read_text().splitlines(), ["primary:default:worker-base"])

        self.user_dir.joinpath("roles.json").unlink()
        calls.unlink()
        for target in (("--review", "council"), ("--review", "default"), ("--all",)):
            with self.subTest(target=target):
                result = self.run_preflight(*target, "--profile", "fallback")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("skipping", result.stdout.lower())
                self.assertFalse(calls.exists())

    def test_run_stdin_vs_arg_non_git_and_extra_dir_args(self):
        argv_log = self.base / "argv.json"
        self.make_executable(
            self.bin / "stubcli",
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "stdin_data = sys.stdin.read() if not sys.stdin.isatty() else ''\n"
            "with open(os.environ['ARGV_LOG'], 'w') as f:\n"
            "    json.dump({'argv': sys.argv[1:], 'stdin': stdin_data, 'env': os.environ.get('CUSTOM_VAR')}, f)\n"
            "print('stub ok')\n",
        )
        self.write_user_config({
            "roles": {
                "stdin-role": {
                    "readonly": False,
                    "workspace": "inherit",
                    "prompt": None,
                    "mode": "cli",
                    "cli": {
                        "provider": "openai",
                        "binary": "stubcli",
                        "args": ["--cwd", "{workdir}"],
                        "nonGitArgs": ["--no-git"],
                        "extraDirArgs": ["--add-dir", "{dir}"],
                        "promptInput": "stdin",
                        "stdinArg": "-",
                        "env": {"CUSTOM_VAR": "hello"},
                        "outerModel": "gemini-3.8-flash-medium",
                    },
                },
                "arg-role": {
                    "readonly": False,
                    "workspace": "inherit",
                    "prompt": None,
                    "mode": "cli",
                    "cli": {
                        "provider": "anthropic",
                        "binary": "stubcli",
                        "args": ["--cwd", "{workdir}"],
                        "nonGitArgs": ["--no-git"],
                        "promptInput": "arg",
                        "promptPrefixArgs": ["-p"],
                        "outerModel": "gemini-3.8-flash-medium",
                    },
                },
            },
        })
        extra = self.base / "extra_dir"
        extra.mkdir()
        env = dict(self.env, ARGV_LOG=str(argv_log))

        # Non-Git directory: stdin mode appends --no-git, --add-dir, -
        res1 = self.run_runner(
            "--role", "stdin-role", "--cwd", self.workdir, "--add-dir", extra,
            stdin_text="build feature\n", env=env,
        )
        self.assertEqual(res1.returncode, 0, res1.stderr)
        _, payload1 = self.parse_runner_output(res1.stdout)
        self.assertEqual(payload1["exitCode"], 0)
        recorded1 = json.loads(argv_log.read_text())
        self.assertEqual(
            recorded1["argv"],
            ["--cwd", str(self.workdir), "--no-git", "--add-dir", str(extra), "-"],
        )
        self.assertEqual(recorded1["stdin"], "build feature\n")
        self.assertEqual(recorded1["env"], "hello")

        # --add-dir on role without extraDirArgs -> exit 2
        bad_add_dir = self.run_runner(
            "--role", "arg-role", "--cwd", self.workdir, "--add-dir", extra,
            stdin_text="task", env=env,
        )
        self.assertEqual(bad_add_dir.returncode, 2)

        # Empty stdin prompt -> exit 2
        empty_prompt = self.run_runner("--role", "arg-role", "--cwd", self.workdir, stdin_text="   \n", env=env)
        self.assertEqual(empty_prompt.returncode, 2)

        # Git directory: arg mode omits --no-git, passes -p <prompt> with empty stdin
        self.init_git_repo()
        self.commit_file("tracked.txt", "init\n")
        res2 = self.run_runner("--role", "arg-role", "--cwd", self.workdir, stdin_text="analyze repo", env=env)
        self.assertEqual(res2.returncode, 0, res2.stderr)
        recorded2 = json.loads(argv_log.read_text())
        self.assertEqual(recorded2["argv"], ["--cwd", str(self.workdir), "-p", "analyze repo"])
        self.assertEqual(recorded2["stdin"], "")

    def test_baseline_mismatch_exits_2_without_running_stub(self):
        marker_file = self.base / "stub_ran"
        self.make_executable(
            self.bin / "codex",
            f"#!/bin/bash\ntouch {shlex.quote(str(marker_file))}\nprintf 'ran\\n'\n",
        )
        self.init_git_repo()
        self.write(self.workdir / ".gitignore", ".agents/\n")
        self.commit_file(".gitignore", ".agents/\n")
        head = self.commit_file("a.txt", "hello\n")
        a_sha1 = hashlib.sha1(b"hello\n").hexdigest()

        # Wrong gitCleanHead
        r1 = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({"gitCleanHead": "0" * 40}),
        )
        self.assertEqual(r1.returncode, 2)
        self.assertFalse(marker_file.exists())

        # Dirty git worktree fails gitCleanHead
        self.write(self.workdir / "untracked.txt", "dirty\n")
        r2 = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({"gitCleanHead": head}),
        )
        self.assertEqual(r2.returncode, 2)
        self.assertFalse(marker_file.exists())
        (self.workdir / "untracked.txt").unlink()

        # Wrong fileSha1
        r3 = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({"fileSha1": {"a.txt": "0" * 40}}),
        )
        self.assertEqual(r3.returncode, 2)
        self.assertFalse(marker_file.exists())

        # plannedNew exists (including dangling symlink)
        dangling = self.workdir / "dangling"
        dangling.symlink_to(self.base / "nonexistent")
        r4 = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({"plannedNew": ["dangling"]}),
        )
        self.assertEqual(r4.returncode, 2)
        self.assertFalse(marker_file.exists())
        dangling.unlink()

        # Matching baseline runs stub
        r5 = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({
                "gitCleanHead": head,
                "fileSha1": {"a.txt": a_sha1},
                "plannedNew": ["new.txt"],
                "allowedPaths": ["a.txt", "new.txt"],
            }),
        )
        self.assertEqual(r5.returncode, 0, r5.stderr)
        self.assertTrue(marker_file.exists())

    def test_readonly_violation_exit_4_and_empty_output_exit_5(self):
        self.init_git_repo()
        self.write(self.workdir / ".gitignore", ".agents/\n")
        self.commit_file(".gitignore", ".agents/\n")
        self.commit_file("keep.txt", "original\n")

        # Readonly violation -> exit 4
        self.make_executable(
            self.bin / "codex",
            "#!/bin/bash\n"
            "printf 'tamper\\n' >> keep.txt\n"
            "printf 'review done\\n'\n",
        )
        violation = self.run_runner("--role", "reviewer-codex", "--cwd", self.workdir)
        self.assertEqual(violation.returncode, 4, (violation.stdout, violation.stderr))
        _, payload = self.parse_runner_output(violation.stdout)
        self.assertTrue(payload["readonlyViolation"])
        self.assertEqual(payload["changedFiles"], ["keep.txt"])
        self.write(self.workdir / "keep.txt", "original\n")

        # Readonly + exit 0 + empty output -> exit 5
        self.make_executable(self.bin / "codex", "#!/bin/bash\nprintf '   \\n'\n")
        empty = self.run_runner("--role", "reviewer-codex", "--cwd", self.workdir)
        self.assertEqual(empty.returncode, 5, (empty.stdout, empty.stderr))
        _, empty_payload = self.parse_runner_output(empty.stdout)
        self.assertTrue(empty_payload["emptyOutput"])
        self.assertFalse(empty_payload["readonlyViolation"])

        # Readonly + non-zero exit + empty output -> exit 1 (real exitCode in JSON)
        self.make_executable(self.bin / "codex", "#!/bin/bash\nexit 9\n")
        nonzero = self.run_runner("--role", "reviewer-codex", "--cwd", self.workdir)
        self.assertEqual(nonzero.returncode, 1, (nonzero.stdout, nonzero.stderr))
        _, nz_payload = self.parse_runner_output(nonzero.stdout)
        self.assertEqual(nz_payload["exitCode"], 9)
        self.assertTrue(nz_payload["emptyOutput"])

    def test_allowed_paths_out_of_scope_and_special_filenames(self):
        self.init_git_repo()
        self.write(self.workdir / ".gitignore", ".agents/\n")
        self.commit_file(".gitignore", ".agents/\n")
        self.commit_file("pre dirty.txt", "v1\n")
        # Already dirty before run; untouched during run -> must not appear in changedFiles
        self.write(self.workdir / "pre dirty.txt", "v2\n")

        self.make_executable(
            self.bin / "codex",
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "Path('src').mkdir(exist_ok=True)\n"
            "Path('src/ok.py').write_text('x = 1\\n')\n"
            "Path('bad file.txt').write_text('oops\\n')\n"
            "print('updated files')\n",
        )
        res = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({"allowedPaths": ["src"]}),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        _, payload = self.parse_runner_output(res.stdout)
        self.assertEqual(payload["changedFiles"], ["bad file.txt", "src/ok.py"])
        self.assertEqual(payload["outOfScopeFiles"], ["bad file.txt"])

    def test_timeout_kills_process_group_without_orphans(self):
        pid_file = self.base / "grandchild.pid"
        self.make_executable(
            self.bin / "codex",
            "#!/bin/bash\n"
            "sleep 30 &\n"
            f"printf '%s\\n' \"$!\" > {shlex.quote(str(pid_file))}\n"
            "sleep 30\n",
        )
        res = self.run_runner("--role", "worker", "--cwd", self.workdir, "--timeout", "1")
        self.assertEqual(res.returncode, 124, (res.stdout, res.stderr))
        _, payload = self.parse_runner_output(res.stdout)
        self.assertTrue(payload["timedOut"])
        self.assertTrue(pid_file.is_file())
        grandchild_pid = int(pid_file.read_text().strip())
        # Wait briefly for kernel process reaping
        deadline = time.monotonic() + 2.0
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild_pid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                alive = False
                break
        self.assertFalse(alive, f"orphan grandchild process {grandchild_pid} still alive")

    def test_large_output_truncation_preserves_head_tail_and_json_trailer(self):
        self.make_executable(
            self.bin / "codex",
            "#!/usr/bin/env python3\n"
            "print('HEAD_MARKER_' + 'A' * 2000 + '_TAIL_MARKER')\n",
        )
        res = self.run_runner("--role", "worker", "--cwd", self.workdir, "--max-output-bytes", "300")
        self.assertEqual(res.returncode, 0, res.stderr)
        body, payload = self.parse_runner_output(res.stdout)
        self.assertIn("HEAD_MARKER_", body)
        self.assertIn("_TAIL_MARKER", body)
        self.assertIn("truncated", body)
        self.assertLessEqual(len(body.encode("utf-8")), 310)
        self.assertEqual(payload["role"], "worker")
        self.assertFalse(payload["emptyOutput"])

    def test_wrapper_symlinks_and_missing_python3(self):
        self.make_executable(self.bin / "codex", "#!/bin/bash\nprintf 'Logged in\\n'\n")
        for script in (PREFLIGHT, RUNNER):
            link = self.home / f"link-{script.name}"
            link.symlink_to(script)
            if script == PREFLIGHT:
                res = self.run_preflight("--role", "worker", executable=link)
            else:
                res = self.run_runner("--role", "worker", "--cwd", self.workdir, executable=link)
            self.assertEqual(res.returncode, 0, (res.stdout, res.stderr))
            no_py = subprocess.run(
                ["/bin/bash", str(script), "--help"],
                cwd=self.workdir,
                env=dict(self.env, PATH=str(self.base)),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(no_py.returncode, 127)
            self.assertIn("python3", no_py.stderr)

    def test_baseline_strict_keys_dot_allowed_paths_symlink_escape_and_unreadable(self):
        marker_file = self.base / "stub_ran_strict"
        self.make_executable(
            self.bin / "codex",
            f"#!/bin/bash\ntouch {shlex.quote(str(marker_file))}\n"
            "printf 'wrote\\n' > sub/out.txt\nprintf 'done\\n'\n",
        )
        (self.workdir / "sub").mkdir()
        secret = self.write(self.base / "outside.txt", "secret\n")
        linkdir = self.workdir / "linkdir"
        linkdir.symlink_to(self.base)
        secret_sha1 = hashlib.sha1(b"secret\n").hexdigest()
        local_file = self.write(self.workdir / "unreadable.txt", "secret\n")
        local_file.chmod(0)
        self.addCleanup(local_file.chmod, 0o600)
        direct_link = self.workdir / "direct_link.txt"
        direct_link.symlink_to(self.write(self.workdir / "real.txt", "real\n"))
        real_sha1 = hashlib.sha1(b"real\n").hexdigest()

        # Unknown key -> exit 2, stub not executed
        r_unknown = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({"head": "abc", "allowedPaths": ["."]}),
        )
        self.assertEqual(r_unknown.returncode, 2)
        self.assertIn("unknown --baseline-json keys", r_unknown.stderr)
        self.assertFalse(marker_file.exists())

        # Intermediate symlink escape in fileSha1 -> exit 2, stub not executed
        r_sym_sha = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({"fileSha1": {"linkdir/outside.txt": secret_sha1}}),
        )
        self.assertEqual(r_sym_sha.returncode, 2)
        self.assertIn("symlink", r_sym_sha.stderr)
        self.assertFalse(marker_file.exists())

        # Intermediate symlink escape in plannedNew -> exit 2, stub not executed
        r_sym_new = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({"plannedNew": ["linkdir/new_outside.txt"]}),
        )
        self.assertEqual(r_sym_new.returncode, 2)
        self.assertIn("symlink", r_sym_new.stderr)
        self.assertFalse(marker_file.exists())

        # Direct symlink target in fileSha1 -> exit 2, stub not executed
        r_direct_sym = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({"fileSha1": {"direct_link.txt": real_sha1}}),
        )
        self.assertEqual(r_direct_sym.returncode, 2)
        self.assertFalse(marker_file.exists())

        # Dot '.' rejected in fileSha1 and plannedNew -> exit 2
        for bad_baseline in ({"fileSha1": {".": real_sha1}}, {"plannedNew": ["."]}):
            r_dot = self.run_runner(
                "--role", "worker", "--cwd", self.workdir,
                "--baseline-json", json.dumps(bad_baseline),
            )
            self.assertEqual(r_dot.returncode, 2)
            self.assertFalse(marker_file.exists())

        # Unreadable fileSha1 (chmod 000) -> exit 2 (not 1), stub not executed
        r_unreadable = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({"fileSha1": {"unreadable.txt": secret_sha1}}),
        )
        self.assertEqual(r_unreadable.returncode, 2)
        self.assertIn("cannot read", r_unreadable.stderr)
        self.assertFalse(marker_file.exists())

        # allowedPaths: ["."] permits writes anywhere in workdir
        r_allowed_dot = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({"allowedPaths": ["."]}),
        )
        self.assertEqual(r_allowed_dot.returncode, 0, r_allowed_dot.stderr)
        self.assertTrue(marker_file.exists())
        _, payload = self.parse_runner_output(r_allowed_dot.stdout)
        self.assertEqual(payload["changedFiles"], ["sub/out.txt"])
        self.assertEqual(payload["outOfScopeFiles"], [])

    def test_non_git_readonly_violation_and_normal_write(self):
        # Non-Git readonly violation -> exit 4
        self.write(self.workdir / "data.txt", "v1\n")
        self.make_executable(
            self.bin / "codex",
            "#!/bin/bash\nprintf 'v2\\n' > data.txt\nprintf 'reviewed\\n'\n",
        )
        ro = self.run_runner("--role", "reviewer-codex", "--cwd", self.workdir)
        self.assertEqual(ro.returncode, 4, (ro.stdout, ro.stderr))
        _, ro_payload = self.parse_runner_output(ro.stdout)
        self.assertTrue(ro_payload["readonlyViolation"])
        self.assertEqual(ro_payload["changedFiles"], ["data.txt"])

        # Non-Git normal write -> exit 0, changedFiles + outOfScopeFiles reported
        self.make_executable(
            self.bin / "codex",
            "#!/bin/bash\nprintf 'ok\\n' > allowed.txt\nprintf 'bad\\n' > extra.txt\nprintf 'done\\n'\n",
        )
        rw = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({"allowedPaths": ["allowed.txt"]}),
        )
        self.assertEqual(rw.returncode, 0, rw.stderr)
        _, rw_payload = self.parse_runner_output(rw.stdout)
        self.assertEqual(rw_payload["changedFiles"], ["allowed.txt", "extra.txt"])
        self.assertEqual(rw_payload["outOfScopeFiles"], ["extra.txt"])

    def test_git_deleted_renamed_and_unauthorized_commit_detection(self):
        self.init_git_repo()
        self.write(self.workdir / ".gitignore", ".agents/\n")
        self.commit_file(".gitignore", ".agents/\n")
        self.commit_file("to_delete.txt", "bye\n")
        self.commit_file("old_name.txt", "rename me\n")

        # 1. Tracked file deletion + staged rename (R)
        git_bin = shutil.which("git")
        self.make_executable(
            self.bin / "codex",
            f"#!/bin/bash\nrm -f to_delete.txt\n{shlex.quote(git_bin)} mv old_name.txt new_name.txt\nprintf 'mutated\\n'\n",
        )
        res = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({"allowedPaths": ["new_name.txt"]}),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        _, payload = self.parse_runner_output(res.stdout)
        self.assertEqual(payload["changedFiles"], ["new_name.txt", "old_name.txt", "to_delete.txt"])
        self.assertEqual(payload["outOfScopeFiles"], ["old_name.txt", "to_delete.txt"])

        # Commit those cleanly, then test unauthorized git commit (HEAD change) in readonly mode
        subprocess.run([git_bin, "-C", str(self.workdir), "add", "-A"], check=True, capture_output=True)
        subprocess.run([git_bin, "-C", str(self.workdir), "commit", "-m", "prep"], check=True, capture_output=True)
        self.make_executable(
            self.bin / "codex",
            "#!/bin/bash\n"
            "printf 'committed\\n' > committed.txt\n"
            f"{shlex.quote(git_bin)} add committed.txt\n"
            f"{shlex.quote(git_bin)} commit -m 'rogue' >/dev/null\n"
            "printf 'looks clean\\n'\n",
        )
        ro_commit = self.run_runner("--role", "reviewer-codex", "--cwd", self.workdir)
        self.assertEqual(ro_commit.returncode, 4, (ro_commit.stdout, ro_commit.stderr))
        _, commit_payload = self.parse_runner_output(ro_commit.stdout)
        self.assertTrue(commit_payload["readonlyViolation"])
        self.assertIn(".git/HEAD", commit_payload["changedFiles"])

        # Same unauthorized commit in writable role with allowedPaths: ["."] flags .git/HEAD as out of scope
        self.make_executable(
            self.bin / "codex",
            "#!/bin/bash\n"
            "printf 'committed2\\n' > committed2.txt\n"
            f"{shlex.quote(git_bin)} add committed2.txt\n"
            f"{shlex.quote(git_bin)} commit -m 'rogue2' >/dev/null\n"
            "printf 'done\\n'\n",
        )
        rw_commit = self.run_runner(
            "--role", "worker", "--cwd", self.workdir,
            "--baseline-json", json.dumps({"allowedPaths": ["."]}),
        )
        self.assertEqual(rw_commit.returncode, 0, rw_commit.stderr)
        _, rw_commit_payload = self.parse_runner_output(rw_commit.stdout)
        self.assertIn(".git/HEAD", rw_commit_payload["changedFiles"])
        self.assertEqual(rw_commit_payload["outOfScopeFiles"], [".git/HEAD"])

    def test_wrappers_do_not_create_pycache_without_env_override(self):
        pycache = ROOT / "scripts" / "lib" / "__pycache__"
        if pycache.exists():
            shutil.rmtree(pycache)
        self.make_executable(self.bin / "codex", "#!/bin/bash\nprintf 'Logged in\\n'\n")
        clean_env = dict(self.env)
        clean_env.pop("PYTHONDONTWRITEBYTECODE", None)

        r_resolve = subprocess.run(
            [str(ROOT / "scripts" / "resolve-roles.sh"), "validate"],
            cwd=self.workdir, env=clean_env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(r_resolve.returncode, 0, r_resolve.stderr)
        r_pre = self.run_preflight("--role", "reviewer-codex", env=clean_env)
        self.assertEqual(r_pre.returncode, 0, r_pre.stderr)
        r_run = self.run_runner("--role", "reviewer-codex", "--cwd", self.workdir, env=clean_env)
        self.assertEqual(r_run.returncode, 0, r_run.stderr)
        self.assertFalse(pycache.exists(), f"unexpected bytecode cache created at {pycache}")

    def test_project_cli_boundary_in_preflight_and_runner(self):
        self.make_executable(self.bin / "codex", "#!/bin/bash\nprintf 'Logged in\\n'\n")
        for profile, overrides in [
            ("default", {"args": ["--project"]}),
            ("rework", {"rework": {"args": ["--project"]}}),
            ("fallback", {"fallback": {"args": ["--project"]}}),
            ("rework", {"rework": None}),
            ("default", {"fallback": None}),
        ]:
            self.write_project_config({"roles": {"worker": {"cli": overrides}}})
            for command in (self.run_preflight, self.run_runner):
                with self.subTest(overrides=overrides, command=command.__name__):
                    args = ("--role", "worker", "--cwd", self.workdir, "--profile", profile)
                    denied = command(*args)
                    self.assertEqual(denied.returncode, 1, denied.stderr)
                    self.assertIn("without --allow-project-cli", denied.stderr)
                    allowed = command(*args, "--allow-project-cli")
                    self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_resolve_binary_returns_absolute_path_across_cwd_change(self):
        relbin = self.base / "relbin"
        relbin.mkdir()
        stub = self.make_executable(relbin / "codex", "#!/bin/bash\nprintf 'Logged in\\n'\n")
        env = dict(self.env, PATH=f"relbin:{self.bin}")
        # Caller cwd is self.base (so PATH "relbin" resolves relative to self.base),
        # while --cwd is self.workdir (a different directory where "relbin" does not exist).
        res = self.run_runner("--role", "worker", "--cwd", self.workdir, cwd=self.base, env=env)
        self.assertEqual(res.returncode, 0, (res.stdout, res.stderr))
        _, payload = self.parse_runner_output(res.stdout)
        self.assertEqual(payload["binary"], str(stub))
        self.assertTrue(os.path.isabs(payload["binary"]))

    def test_baseline_rejects_explicit_null_keys(self):
        marker_file = self.base / "stub_ran_null"
        self.make_executable(
            self.bin / "codex",
            f"#!/bin/bash\ntouch {shlex.quote(str(marker_file))}\nprintf 'ran\\n'\n",
        )
        for key in ("gitCleanHead", "fileSha1", "plannedNew", "allowedPaths"):
            with self.subTest(key=key):
                res = self.run_runner(
                    "--role", "worker", "--cwd", self.workdir,
                    "--baseline-json", json.dumps({key: None}),
                )
                self.assertEqual(res.returncode, 2, (res.stdout, res.stderr))
                self.assertIn(key, res.stderr)
                self.assertFalse(marker_file.exists())

    def test_preflight_sigterm_kills_process_group_and_returns_143(self):
        pid_file = self.base / "pre_grandchild.pid"
        ready_file = self.base / "pre_ready"
        self.make_executable(
            self.bin / "codex",
            "#!/bin/bash\n"
            "sleep 30 &\n"
            f"printf '%s\\n' \"$!\" > {shlex.quote(str(pid_file))}\n"
            f"touch {shlex.quote(str(ready_file))}\n"
            "sleep 30\n",
        )
        proc = subprocess.Popen(
            [str(PREFLIGHT), "--role", "worker", "--cwd", str(self.workdir)],
            cwd=self.workdir,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5.0
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready_file.exists(), "preflight stub did not start in time")
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=8.0)
        self.assertEqual(proc.returncode, 143, (stdout, stderr))

        grandchild_pid = int(pid_file.read_text().strip())
        reap_deadline = time.monotonic() + 2.0
        alive = True
        while time.monotonic() < reap_deadline:
            try:
                os.kill(grandchild_pid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                alive = False
                break
        self.assertFalse(alive, f"orphan preflight grandchild {grandchild_pid} still alive")


if __name__ == "__main__":
    unittest.main()
