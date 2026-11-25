import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "resolve-roles.sh"


def custom_cli_role(
    *,
    provider="pi-ai",
    binary="pi",
    prompt_input="arg",
    outer_model="gemini-3.8-flash-medium",
    **extra,
):
    cfg = {
        "provider": provider,
        "binary": binary,
        "promptInput": prompt_input,
        "outerModel": outer_model,
    }
    cfg.update(extra)
    return {
        "readonly": False,
        "workspace": "inherit",
        "prompt": None,
        "mode": "cli",
        "cli": cfg,
    }


def custom_mcp_role(
    *,
    provider="anthropic",
    **extra,
):
    cfg = {"provider": provider}
    cfg.update(extra)
    return {
        "readonly": False,
        "workspace": "inherit",
        "prompt": None,
        "mode": "mcp",
        "mcp": cfg,
    }


class ResolveRolesTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(CLI.is_file(), "scripts/resolve-roles.sh has not been implemented")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base / "home"
        self.user_dir = self.home / ".gemini" / "config" / "gbstack"
        self.repo = self.home / "project"
        self.project_dir = self.repo / ".agents" / "gbstack"
        self.repo.mkdir(parents=True)
        self.user_config = self.user_dir / "roles.json"
        self.project_config = self.project_dir / "roles.json"
        self.env = dict(
            os.environ,
            HOME=str(self.home),
            GBSTACK_USER_CONFIG_DIR="",
            GBSTACK_PROJECT_CONFIG_DIR="",
            PYTHONDONTWRITEBYTECODE="1",
        )
        self.env.pop("GBSTACK_DEFAULT_CONFIG", None)
        self.env.pop("GBSTACK_ALLOW_PROJECT_CLI", None)

    def invoke(self, *args, env=None, cwd=None, executable=CLI):
        return subprocess.run(
            [str(executable), *map(str, args)],
            cwd=self.repo if cwd is None else cwd,
            env=self.env if env is None else env,
            capture_output=True,
            text=True,
            check=False,
        )

    def success(self, *args, **kwargs):
        result = self.invoke(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def failure(self, result, code, *details):
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr)
        for detail in details:
            self.assertIn(str(detail), result.stderr)

    def write(self, path, contents):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def config(self, path, contents):
        return self.write(path, json.dumps(contents))

    def override(self, contents, *args):
        self.config(self.project_config, contents)
        return self.invoke("--project-config-dir", self.project_dir, *args)

    def invalid(self, contents, *details):
        self.failure(self.override(contents, "validate"), 1, "project", self.project_config, *details)

    def invalid_user(self, contents, *details):
        self.config(self.user_config, contents)
        self.failure(
            self.invoke("--user-config-dir", self.user_dir, "validate"),
            1, "user", self.user_config, *details,
        )

    def role(self, role_id, **fields):
        return {"roles": {role_id: fields}}

    def test_builtin_commands_and_schema_v2_defaults(self):
        result = self.success("validate")
        self.assertEqual(result, {
            "ok": True,
            "sources": {
                "default": str(ROOT / "config" / "roles.default.json"),
                "user": None,
                "project": None,
            },
            "roleCount": 8,
            "reviewCount": 2,
            "cliRoleCount": 2,
        })
        dump = self.success("dump")
        self.assertEqual(dump["schemaVersion"], 2)
        self.assertEqual(dump["pluginRoot"], str(ROOT))
        self.assertEqual(dump["sources"], result["sources"])
        self.assertEqual(list(dump["roles"]), [
            "coordinator", "scout", "plan", "worker", "reviewer-gemini",
            "reviewer-claude", "reviewer-codex", "monitor",
        ])
        self.assertEqual(dump["slots"], {
            "coordinator": "coordinator", "recon": "scout", "plan": "plan",
            "implement": "worker", "monitor": "monitor", "review": "default",
            "interrogate": "default", "council": "council",
        })
        self.assertEqual(dump["cliRoles"], [
            {
                "id": "worker",
                "provider": "openai",
                "binary": "codex",
                "readonly": False,
                "hasPreflight": True,
                "hasRework": False,
                "hasFallback": False,
            },
            {
                "id": "reviewer-codex",
                "provider": "openai",
                "binary": "codex",
                "readonly": True,
                "hasPreflight": True,
                "hasRework": False,
                "hasFallback": False,
            },
        ])
        for role_id in ("worker", "reviewer-codex"):
            cli = dump["roles"][role_id]["cli"]
            self.assertIsNone(cli["rework"])
            self.assertIsNone(cli["fallback"])
        worker = self.success("role", "worker")
        self.assertEqual(worker, dump["roles"]["worker"])
        self.assertEqual(worker["id"], "worker")
        self.assertEqual(worker["effectiveProvider"], "openai")
        self.assertTrue(worker["requiresCliPreflight"])
        self.assertEqual(worker["cli"]["outerModel"], "gemini-3.8-flash-medium")
        self.assertEqual(worker["cli"]["timeoutSeconds"], 600)
        self.assertEqual(worker["cli"]["preflight"]["timeoutSeconds"], 30)
        self.assertEqual(worker["cli"]["preflight"]["env"], {})
        self.assertEqual(worker["cli"]["env"], {})
        self.assertEqual(worker["cli"]["promptPrefixArgs"], [])
        self.assertEqual(worker["native"]["reworkModel"], "gemini-4-neon-high")
        self.assertEqual(worker["promptPath"], str(ROOT / "agents" / "worker.md"))
        self.assertEqual(worker["promptSource"], "default")
        self.assertEqual(self.success("slot", "implement"), {
            "slot": "implement", "kind": "role", "target": "worker", "role": worker,
        })
        coordinator = dump["roles"]["coordinator"]
        self.assertIsNone(coordinator["promptPath"])
        self.assertIsNone(coordinator["promptSource"])
        self.assertEqual(coordinator["native"]["reworkModel"], coordinator["native"]["model"])
        self.assertFalse(coordinator["requiresCliPreflight"])
        self.assertEqual(dump["roles"]["monitor"]["skill"], "swarm_monitor")

        for name, providers, cli_roles in [
            ("default", ["google", "anthropic"], []),
            ("council", ["openai", "google", "anthropic"], ["reviewer-codex"]),
        ]:
            with self.subTest(review=name):
                review = self.success("review", name)
                self.assertEqual(review["id"], name)
                self.assertEqual(review["distinctProviders"], providers)
                self.assertEqual(review["requiresCliPreflight"], bool(cli_roles))
                self.assertEqual(review["cliRoles"], cli_roles)
                self.assertEqual(review["memberRoles"], [dump["roles"][member] for member in review["members"]])

        council = self.success("review", "council")
        self.assertEqual(self.success("slot", "council"), {
            "slot": "council", "kind": "review", "target": "council", "review": council,
        })
        self.assertEqual(council["maxRounds"], 3)

    def test_v1_migration_errors(self):
        self.invalid({"schemaVersion": 1}, "$.schemaVersion", "schemaVersion 2", "cli")
        self.invalid(self.role("worker", mode="codex"), "$.roles.worker.mode", "cli")
        self.invalid(self.role("worker", codex={"provider": "openai"}), "$.roles.worker.codex", "cli")
        self.invalid({"slots": {"codexReview": "council"}}, "$.slots.codexReview", "schemaVersion 2")

    def test_custom_pi_and_omp_cli_roles_and_normalization(self):
        self.config(self.user_config, {
            "roles": {
                "pi-worker": custom_cli_role(
                    provider="anthropic",
                    binary="pi",
                    prompt_input="arg",
                    promptPrefixArgs=["-p"],
                    args=["--print", "--cwd", "{workdir}"],
                    extraDirArgs=["--add-dir", "{dir}"],
                    env={"PI_TELEMETRY": "0"},
                ),
                "omp-worker": custom_cli_role(
                    provider="openai",
                    binary="/opt/omp/bin/omp",
                    fallbackBinaries=["omp"],
                    prompt_input="stdin",
                    stdinArg="--stdin",
                    nonGitArgs=["--no-git"],
                    timeoutSeconds=120,
                    preflight={
                        "command": ["{binary}", "--version"],
                        "outputContains": "omp",
                        "env": {"OMP_CHECK": "1"},
                        "timeoutSeconds": 10,
                    },
                ),
            },
        })
        options = ("--user-config-dir", self.user_dir)
        pi_role = self.success(*options, "role", "pi-worker")
        self.assertEqual(pi_role["effectiveProvider"], "anthropic")
        self.assertTrue(pi_role["requiresCliPreflight"])
        self.assertEqual(pi_role["cli"]["fallbackBinaries"], [])
        self.assertEqual(pi_role["cli"]["nonGitArgs"], [])
        self.assertEqual(pi_role["cli"]["promptPrefixArgs"], ["-p"])
        self.assertEqual(pi_role["cli"]["env"], {"PI_TELEMETRY": "0"})
        self.assertEqual(pi_role["cli"]["timeoutSeconds"], 600)
        self.assertIsNone(pi_role["cli"]["preflight"])
        self.assertFalse(pi_role["cli"]["fallbackToNativeOnPreflightFail"])
        self.assertFalse(pi_role["cli"]["fallbackToNativeOnTransientFailure"])

        omp_role = self.success(*options, "role", "omp-worker")
        self.assertEqual(omp_role["cli"]["timeoutSeconds"], 120)
        self.assertEqual(omp_role["cli"]["preflight"], {
            "command": ["{binary}", "--version"],
            "outputContains": "omp",
            "env": {"OMP_CHECK": "1"},
            "timeoutSeconds": 10,
        })

        cli_roles = self.success(*options, "cli-roles")
        self.assertEqual([entry["id"] for entry in cli_roles], ["worker", "reviewer-codex", "pi-worker", "omp-worker"])
        self.assertFalse(cli_roles[2]["hasPreflight"])
        self.assertTrue(cli_roles[3]["hasPreflight"])

    def test_cli_roles_subcommand(self):
        entries = self.success("cli-roles")
        self.assertEqual([e["id"] for e in entries], ["worker", "reviewer-codex"])
        lines = self.invoke("cli-roles", "--format", "lines")
        self.assertEqual((lines.returncode, lines.stdout, lines.stderr), (0, "worker\nreviewer-codex\n", ""))

        # Explicit selection: missing -> 3, native without cli -> 1, native with cli -> 0
        self.failure(self.invoke("cli-roles", "missing"), 3, "missing")
        self.failure(self.invoke("cli-roles", "scout"), 1, "scout", "cli")

        self.config(self.project_config, {
            "roles": {
                "worker": {"mode": "native"},
                "reviewer-codex": {"mode": "native", "native": {"provider": "test", "model": "m"}},
            },
        })
        options = ("--project-config-dir", self.project_dir)
        self.assertEqual(self.success(*options, "cli-roles"), [])
        self.assertEqual([e["id"] for e in self.success(*options, "cli-roles", "worker")], ["worker"])

    def test_null_rework_model_uses_native_model(self):
        self.config(self.project_config, self.role("worker", native={"reworkModel": None}))
        worker = self.success("--project-config-dir", self.project_dir, "role", "worker")
        self.assertEqual(worker["native"]["reworkModel"], worker["native"]["model"])

    def test_config_merge_and_asset_priority(self):
        user_prompt = self.write(self.user_dir / "agents" / "worker.md", "user")
        project_prompt = self.write(self.project_dir / "agents" / "worker.md", "project")
        self.config(self.user_config, {
            "roles": {"worker": {"native": {"model": "user-model"}, "cli": {"args": ["--user"], "env": {"A": "1", "B": "2"}}}},
            "reviews": {"default": {"members": ["reviewer-claude", "reviewer-gemini"]}},
        })
        self.config(self.project_config, {
            "roles": {"worker": {"native": {"lowQuotaFallback": None}}},
            "reviews": {"default": {"members": ["reviewer-gemini"], "minDistinctProviders": 1}},
        })
        options = ("--user-config-dir", self.user_dir, "--project-config-dir", self.project_dir)
        result = self.success(*options, "dump")
        worker = result["roles"]["worker"]
        self.assertEqual(worker["native"]["model"], "user-model")
        self.assertEqual(worker["native"]["provider"], "google")
        self.assertIsNone(worker["native"]["lowQuotaFallback"])
        self.assertEqual(worker["cli"]["args"], ["--user"])
        self.assertEqual(worker["cli"]["env"], {"A": "1", "B": "2"})
        self.assertEqual(result["reviews"]["default"]["members"], ["reviewer-gemini"])
        self.assertEqual(result["sources"]["user"], str(self.user_config))
        self.assertEqual(result["sources"]["project"], str(self.project_config))
        self.assertEqual(worker["promptPath"], str(project_prompt))
        self.assertEqual(worker["promptSource"], "project")
        project_prompt.unlink()
        worker = self.success(*options, "role", "worker")
        self.assertEqual(worker["promptPath"], str(user_prompt))
        self.assertEqual(worker["promptSource"], "user")

    def test_null_deletion_recreation_and_named_nulls(self):
        self.config(self.user_config, {
            "roles": {"worker": None, "monitor": None}, "reviews": {"default": None, "council": None},
        })
        self.config(self.project_config, {
            "roles": {"worker": {
                "prompt": None, "readonly": False, "workspace": "inherit", "mode": "native",
                "native": {"provider": "test", "model": "fresh"}, "cli": None,
            }},
            "reviews": {"default": {
                "members": ["reviewer-gemini"], "minDistinctProviders": 1,
                "requireDistinctProviders": True, "allowSameProviderFallback": True,
            }},
            "slots": {"monitor": None, "council": None},
        })
        options = ("--user-config-dir", self.user_dir, "--project-config-dir", self.project_dir)
        dump = self.success(*options, "dump")
        worker = dump["roles"]["worker"]
        self.assertNotIn("description", worker)
        self.assertNotIn("concurrentWorkspace", worker)
        self.assertEqual(worker["native"], {"provider": "test", "model": "fresh", "reworkModel": "fresh"})
        self.assertIsNone(worker["cli"])
        self.assertIsNone(worker["promptPath"])
        self.assertNotIn("description", dump["reviews"]["default"])
        self.assertNotIn("onIncomplete", dump["reviews"]["default"])
        for command, name in [("role", "monitor"), ("review", "council"), ("slot", "monitor"), ("slot", "council")]:
            with self.subTest(command=command, name=name):
                self.failure(self.invoke(*options, command, name), 3, name)

    def test_config_env_flags_and_default_path_override(self):
        self.config(self.user_config, self.role("scout", native={"model": "home"}))
        self.config(self.project_config, self.role("plan", native={"model": "project"}))
        default_env = {key: value for key, value in self.env.items() if not key.startswith("GBSTACK_")}
        self.assertEqual(self.success("role", "scout", env=default_env)["native"]["model"], "home")
        self.assertEqual(self.success("role", "plan", env=default_env)["native"]["model"], "project")
        self.assertEqual(self.success("validate")["sources"]["user"], None)
        override_user = self.base / "other-user"
        self.config(override_user / "roles.json", self.role("scout", native={"model": "explicit"}))
        override_project = self.base / "other-project"
        self.config(override_project / "roles.json", self.role("plan", native={"model": "explicit-project"}))
        env = dict(default_env, GBSTACK_USER_CONFIG_DIR=str(self.user_dir),
                   GBSTACK_PROJECT_CONFIG_DIR=str(self.project_dir))
        for option, directory, role, model in [
            ("--user-config-dir", override_user, "scout", "explicit"),
            ("--project-config-dir", override_project, "plan", "explicit-project"),
        ]:
            with self.subTest(option=option):
                self.assertEqual(self.success(option, directory, "role", role, env=env)["native"]["model"], model)
        for options in [
            ("--no-user-config", "--no-project-config"),
            ("--user-config-dir", "", "--project-config-dir", ""),
        ]:
            self.assertEqual(self.success(*options, "validate", env=env)["sources"], self.success("validate")["sources"])
        default = self.base / "alternate.json"
        config = json.loads((ROOT / "config" / "roles.default.json").read_text(encoding="utf-8"))
        config["roles"]["worker"]["native"]["model"] = "alternate"
        self.config(default, config)
        alternate_env = dict(self.env, GBSTACK_DEFAULT_CONFIG=str(default))
        worker = self.success("role", "worker", env=alternate_env)
        self.assertEqual(worker["native"]["model"], "alternate")
        self.assertEqual(worker["promptPath"], str(ROOT / "agents" / "worker.md"))
        self.assertEqual(self.success("validate", env=alternate_env)["sources"]["default"], str(default))
        self.assertEqual(
            self.success("--default-config", default, "validate",
                         env=dict(alternate_env, GBSTACK_DEFAULT_CONFIG="missing"))["sources"]["default"],
            str(default),
        )

    def test_project_discovery_git_directory_and_git_file(self):
        env = self.env.copy()
        del env["GBSTACK_PROJECT_CONFIG_DIR"]
        for git_file in [False, True]:
            with self.subTest(git_file=git_file):
                repo = self.repo / str(git_file)
                nested = repo / "nested" / "deep"
                nested.mkdir(parents=True)
                if git_file:
                    self.write(repo / ".git", "gitdir: elsewhere\n")
                else:
                    (repo / ".git").mkdir()
                config = self.config(repo / ".agents" / "gbstack" / "roles.json", self.role("scout", native={"model": "root"}))
                self.assertEqual(self.success("--cwd", nested, "validate", env=env)["sources"]["project"], str(config))
                alias = self.base / ("alias-" + str(git_file))
                alias.symlink_to(nested, target_is_directory=True)
                self.assertEqual(self.success("--cwd", alias, "role", "scout", env=env)["native"]["model"], "root")
                nearest = repo / "nested" / ".agents" / "gbstack"
                self.config(nearest / "roles.json", self.role("scout", native={"model": "near"}))
                self.assertEqual(self.success("role", "scout", env=env, cwd=nested)["native"]["model"], "near")

    def test_non_git_probe_and_git_root_bound(self):
        env = self.env.copy()
        del env["GBSTACK_PROJECT_CONFIG_DIR"]
        self.config(self.home / ".agents" / "gbstack" / "roles.json", self.role("scout", native={"model": "above"}))
        self.config(self.project_config, self.role("scout", native={"model": "local"}))
        nested = self.repo / "nested"
        nested.mkdir()
        self.assertIsNone(self.success("--cwd", nested, "validate", env=env)["sources"]["project"])
        self.assertEqual(self.success("--cwd", self.repo, "role", "scout", env=env)["native"]["model"], "local")
        self.project_config.unlink()
        (self.repo / ".git").mkdir()
        self.assertIsNone(self.success("--cwd", nested, "validate", env=env)["sources"]["project"])

    def test_project_probe_prompt_only_ignores_templates(self):
        env = self.env.copy()
        del env["GBSTACK_PROJECT_CONFIG_DIR"]
        (self.repo / ".git").mkdir()
        nested = self.repo / "nested"
        nested.mkdir()
        self.config(self.project_config, self.role("worker", native={"model": "repo"}))
        nearest = nested / ".agents" / "gbstack"
        prompt = self.write(nearest / "agents" / "worker.md", "local prompt")
        worker = self.success("--cwd", nested, "role", "worker", env=env)
        self.assertEqual(worker["promptPath"], str(prompt))
        self.assertEqual(worker["promptSource"], "project")
        self.assertEqual(worker["native"]["model"], "gemini-4-hydrogen-high")
        prompt.unlink()
        prompt.parent.rmdir()
        self.write(nearest / "templates" / "codex" / "worker-codex.config.toml", "")
        self.assertEqual(
            self.success("--cwd", nested, "validate", env=env)["sources"]["project"],
            str(self.project_config),
        )

    def test_custom_roles_and_direct_prompts(self):
        default_prompt = self.write(self.project_dir / "agents" / "custom.md", "custom")
        direct = self.write(self.home / "direct.md", "direct")
        self.config(self.project_config, {
            "roles": {
                "custom": {
                    "readonly": True, "workspace": "inherit", "mode": "native",
                    "native": {"provider": "other", "model": "custom"},
                },
                "scout": {"prompt": "~/direct.md"},
                "plan": {"prompt": str(direct)},
            },
            "slots": {"recon": "custom"},
            "reviews": {"custom": {
                "members": ["custom"], "minDistinctProviders": 1,
                "requireDistinctProviders": True, "allowSameProviderFallback": False,
            }},
        })
        options = ("--project-config-dir", self.project_dir)
        custom = self.success(*options, "slot", "recon")["role"]
        self.assertEqual(custom["prompt"], "custom.md")
        self.assertEqual(custom["promptPath"], str(default_prompt))
        self.assertEqual(custom["native"]["reworkModel"], "custom")
        self.assertEqual(self.success(*options, "review", "custom")["memberRoles"], [custom])
        for name in ["scout", "plan"]:
            self.assertEqual(self.success(*options, "role", name)["promptPath"], str(direct))

    def test_config_file_failures(self):
        for data in ["{", "[]", "null", '{"schemaVersion": NaN}']:
            with self.subTest(data=data):
                self.write(self.project_config, data)
                result = self.invoke("--project-config-dir", self.project_dir, "validate")
                self.failure(result, 1, "project", self.project_config, "$")
        self.project_config.unlink()
        self.project_config.mkdir()
        self.failure(self.invoke("--project-config-dir", self.project_dir, "validate"), 1,
                     "project", self.project_config, "$")
        self.project_config.rmdir()
        self.write(self.project_config, "{}").chmod(0)
        self.failure(self.invoke("--project-config-dir", self.project_dir, "validate"), 1,
                     "project", self.project_config, "$")
        self.project_config.unlink()
        missing = self.base / "missing.json"
        self.failure(self.invoke("--default-config", missing, "validate"), 1, "default", missing, "$")

    def test_schema_unknown_keys_and_type_conflicts(self):
        cases = [
            ({"schemaVersion": 3}, "$.schemaVersion"),
            ({"schemaVersion": True}, "$.schemaVersion"),
            ({"unknown": 1}, "$.unknown"),
            ({"slots": {"unknown": None}}, "$.slots.unknown"),
            (self.role("worker", unknown=True), "$.roles.worker.unknown"),
            (self.role("worker", native={"unknown": True}), "$.roles.worker.native.unknown"),
            ({"reviews": {"default": {"unknown": True}}}, "$.reviews.default.unknown"),
            (self.role("worker", readonly=1), "$.roles.worker.readonly"),
            (self.role("worker", native=[]), "$.roles.worker.native"),
        ]
        user_cases = [
            (self.role("worker", cli={"unknown": True}), "$.roles.worker.cli.unknown"),
            (self.role("worker", cli={"preflight": {"unknown": True}}), "$.roles.worker.cli.preflight.unknown"),
            (self.role("worker", cli={"args": {}}), "$.roles.worker.cli.args"),
        ]
        for config, path in cases:
            with self.subTest(path=path, config=config):
                self.invalid(config, path)
        for config, path in user_cases:
            with self.subTest(path=path, config=config):
                self.invalid_user(config, path)
        self.config(self.user_config, self.role("worker", native={"model": "user"}))
        self.config(self.project_config, self.role("worker", native={"model": 123}))
        self.failure(self.invoke("--user-config-dir", self.user_dir, "--project-config-dir", self.project_dir, "validate"),
                     1, "project", self.project_config, "$.roles.worker.native.model")

    def test_role_ids_and_slot_constraints(self):
        for name in ["all", "coordinator-temp", "UPPER", "x" * 25, "def.bad"]:
            with self.subTest(name=name):
                self.invalid(self.role(name, prompt=None), name)
        cases = [
            ("coordinator", None), ("coordinator", "worker"), ("recon", "worker"),
            ("plan", "worker"), ("plan", "coordinator"), ("implement", "missing"),
            ("review", "missing"), ("interrogate", "worker"),
        ]
        for slot, target in cases:
            with self.subTest(slot=slot, target=target):
                self.invalid({"slots": {slot: target}}, "$.slots." + slot)
        self.failure(self.override({"roles": {"worker": None}}, "validate"), 1, "$.slots.implement")
        self.failure(self.override({"reviews": {"council": None}}, "validate"), 1, "$.slots.council")
        self.invalid(self.role("coordinator", workspace="inherit"), "$.roles.coordinator.workspace")

    def test_execution_mode_and_cli_validation(self):
        project_cases = [
            (self.role("scout", native=None), "$.roles.scout.native"),
            (self.role("worker", cli=None), "$.roles.worker.cli"),
            (self.role("worker", native=None), "$.roles.worker.cli.fallbackToNativeOnPreflightFail"),
        ]
        user_cases = [
            (self.role("worker", cli={"provider": None}), "$.roles.worker.cli.provider"),
            (self.role("worker", cli={"binary": None}), "$.roles.worker.cli.binary"),
            (self.role("worker", cli={"outerModel": ""}), "$.roles.worker.cli.outerModel"),
            (self.role("worker", cli={"promptInput": "file"}), "$.roles.worker.cli.promptInput"),
            (self.role("worker", cli={"promptInput": "arg"}), "$.roles.worker.cli.stdinArg"),
            (self.role("worker", cli={"promptPrefixArgs": ["-p"]}), "$.roles.worker.cli.promptPrefixArgs"),
            (self.role("worker", cli={"fallbackBinaries": [""]}), "$.roles.worker.cli.fallbackBinaries[0]"),
            (self.role("worker", cli={"args": [123]}), "$.roles.worker.cli.args[0]"),
            (self.role("worker", cli={"env": {"": "1"}}), "$.roles.worker.cli.env"),
            (self.role("worker", cli={"env": {"A": 1}}), "$.roles.worker.cli.env.A"),
            (self.role("worker", cli={"env": {"A": None}}), "$.roles.worker.cli.env.A"),
            (self.role("worker", cli={"timeoutSeconds": 0}), "$.roles.worker.cli.timeoutSeconds"),
            (self.role("worker", cli={"timeoutSeconds": True}), "$.roles.worker.cli.timeoutSeconds"),
            (self.role("worker", cli={"preflight": {"command": []}}), "$.roles.worker.cli.preflight.command"),
            (self.role("worker", cli={"preflight": {"command": [""]}}), "$.roles.worker.cli.preflight.command[0]"),
            (self.role("worker", cli={"preflight": {"timeoutSeconds": 0}}), "$.roles.worker.cli.preflight.timeoutSeconds"),
            (self.role("worker", cli={"preflight": {"env": {"A": 1}}}), "$.roles.worker.cli.preflight.env.A"),
            (self.role("worker", native=None, cli={"fallbackToNativeOnPreflightFail": False}),
             "$.roles.worker.cli.fallbackToNativeOnTransientFailure"),
        ]
        for config, path in project_cases:
            with self.subTest(config=config):
                self.invalid(config, path)
        for config, path in user_cases:
            with self.subTest(config=config):
                self.invalid_user(config, path)

    def test_cli_rework_and_fallback_same_binary_inheritance(self):
        self.config(self.user_config, {"roles": {
            "worker": {"cli": {
                "env": {"KEEP": "base", "CHANGE": "base"},
                "preflight": {"env": {"CHECK": "base"}, "timeoutSeconds": 19},
                "rework": {
                    "args": ["--rework"], "env": {"CHANGE": "rework"},
                    "timeoutSeconds": 35, "outerModel": "rework-model",
                },
                "fallback": {
                    "binary": "codex", "args": None, "promptInput": "arg",
                    "promptPrefixArgs": ["-p"], "preflight": {"command": ["{binary}", "--version"]},
                },
            }},
            "arg-role": custom_cli_role(promptPrefixArgs=["-p"], rework={"promptInput": "stdin", "stdinArg": "-"}),
        }})
        dump = self.success("--user-config-dir", self.user_dir, "dump")
        cli = dump["roles"]["worker"]["cli"]
        for field in ("binary", "provider", "fallbackBinaries", "stdinArg", "nonGitArgs", "extraDirArgs", "preflight"):
            self.assertEqual(cli["rework"][field], cli[field], field)
        self.assertEqual(cli["rework"]["args"], ["--rework"])
        self.assertEqual(cli["rework"]["env"], {"KEEP": "base", "CHANGE": "rework"})
        self.assertEqual(cli["rework"]["timeoutSeconds"], 35)
        self.assertEqual(cli["rework"]["outerModel"], "rework-model")
        fallback = cli["fallback"]
        self.assertEqual(fallback["args"], [])
        self.assertIsNone(fallback["stdinArg"])
        self.assertEqual(fallback["promptPrefixArgs"], ["-p"])
        self.assertEqual(fallback["preflight"], {
            "command": ["{binary}", "--version"], "env": {}, "timeoutSeconds": 30,
        })
        arg_rework = dump["roles"]["arg-role"]["cli"]["rework"]
        self.assertEqual(arg_rework["promptPrefixArgs"], [])
        self.assertEqual(arg_rework["stdinArg"], "-")
        self.assertTrue(dump["cliRoles"][0]["hasRework"])
        self.assertTrue(dump["cliRoles"][0]["hasFallback"])

    def test_cli_fallback_cross_binary_isolation(self):
        self.config(self.user_config, self.role("worker", cli={
            "env": {"BASE_ONLY": "yes"}, "fallback": {"binary": "pi"},
        }))
        cli = self.success("--user-config-dir", self.user_dir, "role", "worker")["cli"]
        fallback = cli["fallback"]
        self.assertEqual(fallback["binary"], "pi")
        for field, value in {
            "fallbackBinaries": [], "args": [], "nonGitArgs": [], "extraDirArgs": [],
            "stdinArg": None, "promptPrefixArgs": [], "env": {}, "preflight": None,
        }.items():
            self.assertEqual(fallback[field], value, field)
        for field in ("provider", "promptInput", "timeoutSeconds", "outerModel", "outerLowQuotaFallback"):
            self.assertEqual(fallback[field], cli[field], field)

    def test_cli_fallback_cross_layer_binary_switch_clears_settings(self):
        options = ("--user-config-dir", self.user_dir, "--project-config-dir", self.project_dir,
                   "--allow-project-cli")
        for prompt_input, prompt_args in [("stdin", {"stdinArg": "-"}), ("arg", {"promptPrefixArgs": ["-p"]})]:
            with self.subTest(prompt_input=prompt_input):
                self.config(self.user_config, self.role("worker", cli={"fallback": {
                    "binary": "head", "fallbackBinaries": ["/old/head"], "args": ["-n", "1"],
                    "nonGitArgs": ["--non-git"], "extraDirArgs": ["--dir"],
                    "promptInput": prompt_input, **prompt_args, "env": {"OLD": "1"},
                    "preflight": {"command": ["head", "--v"], "outputContains": "old"},
                }}))
                self.config(self.project_config, self.role("worker", cli={"fallback": {"binary": "wc"}}))
                fallback = self.success(*options, "role", "worker")["cli"]["fallback"]
                self.assertEqual(fallback["binary"], "wc")
                self.assertEqual(fallback["promptInput"], prompt_input)
                for field, value in {
                    "fallbackBinaries": [], "args": [], "nonGitArgs": [], "extraDirArgs": [],
                    "stdinArg": None, "promptPrefixArgs": [], "env": {}, "preflight": None,
                }.items():
                    self.assertEqual(fallback[field], value, field)

    def test_cli_rework_cross_layer_preflight_replacement(self):
        self.config(self.user_config, self.role("worker", cli={"rework": {"preflight": {
            "command": ["login", "status"], "outputContains": "logged in", "timeoutSeconds": 20,
            "env": {"OLD": "1"},
        }}}))
        self.config(self.project_config, self.role("worker", cli={"rework": {"preflight": {
            "command": ["tool", "--version"],
        }}}))
        role = self.success("--user-config-dir", self.user_dir, "--project-config-dir", self.project_dir,
                            "--allow-project-cli", "role", "worker")
        self.assertEqual(role["cli"]["rework"]["preflight"], {
            "command": ["tool", "--version"], "env": {}, "timeoutSeconds": 30,
        })

    def test_cli_rework_cross_layer_prompt_input_switch(self):
        options = ("--user-config-dir", self.user_dir, "--project-config-dir", self.project_dir,
                   "--allow-project-cli")
        for old_input, new_input, field, value, cleared in [
            ("stdin", "arg", "stdinArg", "-", None),
            ("arg", "stdin", "promptPrefixArgs", ["-p"], []),
        ]:
            with self.subTest(old_input=old_input, new_input=new_input):
                self.config(self.user_config, self.role("worker", cli={"rework": {
                    "promptInput": old_input, field: value,
                }}))
                self.config(self.project_config, self.role("worker", cli={"rework": {"promptInput": new_input}}))
                rework = self.success(*options, "role", "worker")["cli"]["rework"]
                self.assertEqual(rework["promptInput"], new_input)
                self.assertEqual(rework[field], cleared)

                self.config(self.project_config, self.role("worker", cli={"rework": {
                    "promptInput": new_input, field: value,
                }}))
                self.failure(self.invoke(*options, "validate"), 1, f"project: {self.project_config}:",
                             f"$.roles.worker.cli.rework.{field}")

    def test_cli_variant_null_clearing_and_cross_layer_merge(self):
        default = json.loads((ROOT / "config" / "roles.default.json").read_text())
        default["roles"]["worker"]["cli"].update(
            env={"BASE": "yes"}, timeoutSeconds=90,
            rework={"args": ["--default-rework"], "env": {"LOW": "yes"}, "timeoutSeconds": 10},
            fallback={},
        )
        default_config = self.config(self.base / "default.json", default)
        options = ("--default-config", default_config, "--user-config-dir", self.user_dir,
                   "--project-config-dir", self.project_dir, "--allow-project-cli")
        self.config(self.user_config, self.role("worker", cli={
            "rework": {"env": {"USER": "yes"}, "timeoutSeconds": None},
        }))
        cli = self.success(*options, "role", "worker")["cli"]
        self.assertEqual(cli["rework"]["args"], ["--default-rework"])
        self.assertEqual(cli["rework"]["env"], {"BASE": "yes", "LOW": "yes", "USER": "yes"})
        self.assertEqual(cli["rework"]["timeoutSeconds"], 90)
        self.assertEqual(cli["fallback"], {
            key: value for key, value in cli.items()
            if key not in ("rework", "fallback") and not key.startswith("fallbackToNative")
        })
        self.config(self.project_config, self.role("worker", cli={
            "rework": {"env": None, "nonGitArgs": None, "preflight": None, "outerLowQuotaFallback": None},
            "fallback": None,
        }))
        cli = self.success(*options, "role", "worker")["cli"]
        for field, value in {"env": {}, "nonGitArgs": [], "preflight": None, "outerLowQuotaFallback": None}.items():
            self.assertEqual(cli["rework"][field], value, field)
        self.assertIsNone(cli["fallback"])
        self.config(self.project_config, self.role("worker", cli={"rework": None}))
        self.assertIsNone(self.success(*options, "role", "worker")["cli"]["rework"])

    def test_cli_variant_validation_and_origin_attribution(self):
        for variant in ("rework", "fallback"):
            path = f"$.roles.worker.cli.{variant}"
            for override, suffix in [
                ([], ""),
                ({"rework": {}}, ".rework"), ({"fallback": None}, ".fallback"),
                ({"fallbackToNativeOnPreflightFail": False}, ".fallbackToNativeOnPreflightFail"),
                ({"fallbackToNativeOnTransientFailure": True}, ".fallbackToNativeOnTransientFailure"),
                ({"unknown": True}, ".unknown"), ({"provider": None}, ".provider"),
                ({"binary": 5}, ".binary"), ({"binary": None}, ".binary"), ({"args": [False]}, ".args[0]"),
                ({"promptInput": "file"}, ".promptInput"),
                ({"promptInput": "arg", "stdinArg": "-"}, ".stdinArg"),
                ({"promptPrefixArgs": ["-p"]}, ".promptPrefixArgs"),
                ({"env": {"BAD": None}}, ".env.BAD"), ({"env": {"": "bad"}}, ".env"),
                ({"env": {"BAD": 4}}, ".env.BAD"), ({"timeoutSeconds": 0}, ".timeoutSeconds"),
                ({"preflight": {"unknown": 1}}, ".preflight.unknown"),
                ({"preflight": {"env": {}}}, ".preflight.command"),
            ]:
                with self.subTest(variant=variant, override=override):
                    self.invalid_user(self.role("worker", cli={variant: override}), path + suffix)
        self.config(self.user_config, self.role("worker", mode="native", cli={
            "rework": {"provider": None}, "fallback": {"binary": None, "promptInput": None, "outerModel": None},
        }))
        options = ("--user-config-dir", self.user_dir)
        cli = self.success(*options, "role", "worker")["cli"]
        self.assertIsNone(cli["rework"]["provider"])
        self.assertIsNone(cli["fallback"]["binary"])
        self.config(self.project_config, self.role("worker", mode="cli"))
        self.failure(self.invoke(*options, "--project-config-dir", self.project_dir, "validate"), 1,
                     "project", self.project_config, "$.roles.worker.cli.rework.provider")

    def test_review_cli_rework_and_fallback_provider_validation(self):
        for variant in ("rework", "fallback"):
            for provider, allow, distinct, minimum, ok in [
                ("openai", False, True, 2, True), ("other", False, True, 2, True),
                ("google", False, True, 2, False), ("google", True, True, 2, variant == "fallback"),
                ("google", False, False, 2, True),
                ("google", True, True, 3, False), ("other", True, True, 3, True),
            ]:
                with self.subTest(variant=variant, provider=provider, allow=allow, distinct=distinct, minimum=minimum):
                    self.config(self.user_config, {
                        **self.role("reviewer-codex", cli={variant: {"provider": provider}}),
                        "reviews": {"council": {
                            "members": ["reviewer-codex", "reviewer-gemini", "reviewer-claude"],
                            "minDistinctProviders": minimum, "requireDistinctProviders": distinct,
                            "allowSameProviderFallback": allow,
                        }},
                    })
                    result = self.invoke("--user-config-dir", self.user_dir, "validate")
                    if ok:
                        self.assertEqual(result.returncode, 0, result.stderr)
                    else:
                        self.failure(result, 1, self.user_config,
                                     f"$.roles.reviewer-codex.cli.{variant}.provider", "council")

    def test_review_cli_simultaneous_variant_switch_provider_validation(self):
        members = ["reviewer-a", "reviewer-b"]
        for variant in ("fallback", "rework"):
            for minimum, allow in [(2, False), (2, True), (1, False), (1, True)]:
                with self.subTest(variant=variant, minimum=minimum, allow=allow):
                    self.config(self.user_config, {
                        "roles": {
                            member: dict(custom_cli_role(provider=provider, **{variant: {"provider": "c"}}),
                                         readonly=True)
                            for member, provider in zip(members, ("a", "b"))
                        },
                        "reviews": {"custom": {
                            "members": members, "minDistinctProviders": minimum,
                            "requireDistinctProviders": True, "allowSameProviderFallback": allow,
                        }},
                    })
                    result = self.invoke("--user-config-dir", self.user_dir, "validate")
                    if variant == "fallback" and allow and minimum == 1:
                        self.assertEqual(result.returncode, 0, result.stderr)
                    else:
                        self.failure(result, 1, f"user: {self.user_config}:",
                                     f"$.roles.reviewer-a.cli.{variant}.provider", "custom")

    def test_review_cli_partial_fallback_collapse_violates_minimum(self):
        self.invalid_user({
            "roles": {
                member: dict(custom_cli_role(provider=base, fallback={"provider": fallback}), readonly=True)
                for member, base, fallback in (
                    ("reviewer-a", "a", "c"),
                    ("reviewer-b", "b", "c"),
                    ("reviewer-c", "c", "d"),
                )
            },
            "reviews": {"custom": {
                "members": ["reviewer-a", "reviewer-b", "reviewer-c"],
                "minDistinctProviders": 2,
                "requireDistinctProviders": True,
                "allowSameProviderFallback": True,
            }},
        }, "$.roles.reviewer-a.cli.fallback.provider", "custom")

    def test_review_cli_mixed_fallback_and_rework_provider_conflict(self):
        self.invalid_user({
            "roles": {
                "reviewer-a": dict(custom_cli_role(provider="a", fallback={"provider": "c"}), readonly=True),
                "reviewer-b": dict(custom_cli_role(provider="b", rework={"provider": "c"}), readonly=True),
            },
            "reviews": {"custom": {
                # Validate rework first so the error points to the rework provider.
                "members": ["reviewer-b", "reviewer-a"],
                "minDistinctProviders": 2,
                "requireDistinctProviders": True,
                "allowSameProviderFallback": False,
            }},
        }, "$.roles.reviewer-b.cli.rework.provider", "custom")

    def test_review_description_does_not_change_provider_conflict_origin(self):
        default = json.loads((ROOT / "config" / "roles.default.json").read_text())
        del default["reviews"]["council"]["allowSameProviderFallback"]
        default_config = self.config(self.base / "default.json", default)
        self.config(self.user_config, self.role("reviewer-codex", cli={"fallback": {"provider": "google"}}))
        self.config(self.project_config, {"reviews": {"council": {"description": "Updated description"}}})
        result = self.invoke("--default-config", default_config, "--user-config-dir", self.user_dir,
                             "--project-config-dir", self.project_dir, "validate")
        self.failure(result, 1, f"user: {self.user_config}:", "$.roles.reviewer-codex.cli.fallback.provider", "council")

    def test_review_member_and_provider_validation(self):
        for members in [[], ["reviewer-gemini", "reviewer-gemini"], ["worker", "reviewer-gemini"],
                        ["coordinator", "reviewer-gemini"], ["missing", "reviewer-gemini"]]:
            with self.subTest(members=members):
                self.invalid({"reviews": {"default": {"members": members}}}, "$.reviews.default.members")
        for minimum in [0, 3]:
            with self.subTest(minimum=minimum):
                self.invalid({"reviews": {"default": {"minDistinctProviders": minimum}}}, "$.reviews.default.minDistinctProviders")
        self.failure(self.override(self.role("reviewer-claude", native={"provider": "google"}), "validate"),
                     1, "$.reviews.default")
        self.invalid(self.role("reviewer-claude", native={"lowQuotaFallback": "fallback"}),
                     "$.roles.reviewer-claude.native.lowQuotaFallbackProvider")
        self.invalid(self.role("reviewer-claude", native={"lowQuotaFallback": "fallback", "lowQuotaFallbackProvider": "google"}),
                     "$.roles.reviewer-claude.native.lowQuotaFallbackProvider")
        for provider in ["anthropic", "other"]:
            with self.subTest(provider=provider):
                result = self.override(self.role("reviewer-claude", native={
                    "lowQuotaFallback": "fallback", "lowQuotaFallbackProvider": provider,
                }), "validate")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_review_rejects_same_provider_fallback_even_when_minimum_is_met(self):
        default = json.loads((ROOT / "config" / "roles.default.json").read_text())
        default["roles"]["reviewer-claude"]["native"].update(
            lowQuotaFallback="fallback", lowQuotaFallbackProvider="openai",
        )
        council = default["reviews"]["council"]
        council["minDistinctProviders"] = 2
        del council["allowSameProviderFallback"]
        default_config = self.config(self.base / "default.json", default)
        options = ("--default-config", default_config)
        field = "$.roles.reviewer-claude.native.lowQuotaFallbackProvider"
        self.failure(self.invoke(*options, "validate"), 1, default_config, field, "council")
        for value in (None, False, True):
            with self.subTest(allowSameProviderFallback=value):
                self.config(self.project_config, {"reviews": {"council": {"allowSameProviderFallback": value}}})
                result = self.invoke(*options, "--project-config-dir", self.project_dir, "validate")
                if value is True:
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    self.failure(result, 1, "project", self.project_config, field, "council")

    def test_review_policy_and_round_validation(self):
        for key, value in (("onMissingProvider", "retry"), ("onIncomplete", "skip"),
                           ("maxRounds", 0), ("maxRounds", -1)):
            with self.subTest(key=key, value=value):
                self.invalid({"reviews": {"council": {key: value}}}, "$.reviews.council." + key)

    def test_prompt_validation_and_shadowing(self):
        for prompt in ["missing.md", "../worker.md", "sub/../../worker.md"]:
            with self.subTest(prompt=prompt):
                self.invalid(self.role("worker", prompt=prompt), "$.roles.worker.prompt")
        leaf = self.project_dir / "agents" / "worker.md"
        leaf.parent.mkdir(parents=True)
        options = ("--project-config-dir", self.project_dir)
        self.project_config.unlink()
        leaf.mkdir()
        self.failure(self.invoke(*options, "validate"), 1, "project", leaf, "$.roles.worker.prompt")
        leaf.rmdir()
        leaf.symlink_to(self.base / "missing")
        self.failure(self.invoke(*options, "validate"), 1, "project", leaf, "$.roles.worker.prompt")
        leaf.unlink()
        self.write(leaf, "").chmod(0)
        self.failure(self.invoke(*options, "validate"), 1, "project", leaf, "$.roles.worker.prompt")
        leaf.chmod(0o600)
        self.assertEqual(self.success(*options, "role", "worker")["promptPath"], str(leaf))

    def test_cli_usage_and_missing_queries(self):
        for args in [(), ("unrecognized",), ("role",), ("slot", "plan", "extra"),
                     ("cli-roles", "--format", "yaml")]:
            with self.subTest(args=args):
                self.failure(self.invoke(*args), 2)
        for command in ["role", "slot", "review"]:
            with self.subTest(command=command):
                self.failure(self.invoke(command, "missing"), 3, "missing")
        invalid = self.override(self.role("worker", prompt="missing.md"), "role", "missing")
        self.failure(invalid, 1, "$.roles.worker.prompt")
        for flag in ["-h", "--help"]:
            result = self.invoke(flag)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("cli-roles", result.stdout)

    def test_wrapper_resolves_symlinks_from_another_directory(self):
        intermediate = self.base / "intermediate"
        intermediate.symlink_to(os.path.relpath(CLI, self.base))
        launcher = self.home / "launcher"
        launcher.symlink_to(intermediate)
        self.assertEqual(self.success("dump", executable=launcher)["pluginRoot"], str(ROOT))
        result = subprocess.run(
            ["/bin/bash", str(CLI), "validate"],
            cwd=self.repo, env=dict(self.env, PATH=str(self.base)), capture_output=True, text=True, check=False,
        )
        self.failure(result, 127, "python3")

    def test_cross_layer_mode_flip_origin_attribution(self):
        # Project layer flips scout to mode=cli without cli config -> blames project, not default
        self.invalid(self.role("scout", mode="cli"), "$.roles.scout.cli")
        # Project layer flips reviewer-codex to mode=native without native config -> blames project
        self.invalid(self.role("reviewer-codex", mode="native"), "$.roles.reviewer-codex.native")
        # User layer provides native with conflicting provider, project layer flips mode=native -> blames project
        self.config(self.user_config, self.role("reviewer-codex", native={"provider": "google", "model": "m"}))
        self.config(self.project_config, self.role("reviewer-codex", mode="native"))
        result = self.invoke("--user-config-dir", self.user_dir, "--project-config-dir", self.project_dir, "validate")
        self.failure(result, 1, "project", self.project_config, "$.reviews.council")

    def test_project_cli_security_boundary(self):
        # Default: project layer cannot define or override non-null cli
        for bad in (
            self.role("worker", cli={"args": ["--evil"]}),
            self.role("worker", cli={}),
            self.role("custom-cli", **custom_cli_role()),
        ):
            with self.subTest(bad=bad):
                self.invalid(bad, "--allow-project-cli")

        # Project layer MAY set cli: null (with mode: native), switch mode between pre-defined modes, or edit native
        self.config(self.project_config, {
            "roles": {
                "worker": {"mode": "native", "cli": None, "native": {"model": "proj-native"}},
            },
        })
        worker_native = self.success("--project-config-dir", self.project_dir, "role", "worker")
        self.assertEqual(worker_native["mode"], "native")
        self.assertIsNone(worker_native["cli"])

        # Project layer MAY define/override cli when --allow-project-cli or GBSTACK_ALLOW_PROJECT_CLI=1 is set
        self.config(self.project_config, {
            "roles": {"pi-worker": custom_cli_role(args=["--proj"])},
        })
        pi_flag = self.success("--project-config-dir", self.project_dir, "--allow-project-cli", "role", "pi-worker")
        self.assertEqual(pi_flag["cli"]["args"], ["--proj"])
        pi_env = self.success(
            "--project-config-dir", self.project_dir, "role", "pi-worker",
            env=dict(self.env, GBSTACK_ALLOW_PROJECT_CLI="1"),
        )
        self.assertEqual(pi_env["cli"]["args"], ["--proj"])

        for variant in ("rework", "fallback"):
            for override in (None, {"args": ["--proj"]}):
                with self.subTest(variant=variant, override=override):
                    self.invalid(self.role("worker", cli={variant: override}), "--allow-project-cli")
                    options = ("--project-config-dir", self.project_dir)
                    for cli in (
                        self.success(*options, "--allow-project-cli", "role", "worker")["cli"],
                        self.success(*options, "role", "worker",
                                     env=dict(self.env, GBSTACK_ALLOW_PROJECT_CLI="1"))["cli"],
                    ):
                        if override is None:
                            self.assertIsNone(cli[variant])
                        else:
                            self.assertEqual(cli[variant]["args"], ["--proj"])

    def test_mcp_role_resolution_and_defaults(self):
        self.config(self.user_config, {
            "roles": {
                "mcp-minimal": custom_mcp_role(provider="anthropic"),
                "mcp-full": {
                    "readonly": False,
                    "workspace": "inherit",
                    "prompt": None,
                    "mode": "mcp",
                    "native": {"provider": "google", "model": "gemini-4-hydrogen-high"},
                    "mcp": {
                        "provider": "openai",
                        "server": "custom_pi_rpc",
                        "model": "gpt-5.4",
                        "thinking": "high",
                        "waitMs": 30000,
                        "outerModel": "gemini-3.8-flash-medium",
                        "outerLowQuotaFallback": "gemini-3.8-flash-low",
                        "fallbackToNativeOnTransientFailure": True,
                        "rework": {"thinking": "max", "waitMs": 60000},
                        "fallback": {"model": "gpt-5.3", "thinking": "medium"},
                    },
                },
            },
        })
        options = ("--user-config-dir", self.user_dir)
        minimal = self.success(*options, "role", "mcp-minimal")
        self.assertEqual(minimal["mode"], "mcp")
        self.assertEqual(minimal["effectiveProvider"], "anthropic")
        self.assertFalse(minimal["requiresCliPreflight"])
        self.assertEqual(minimal["mcp"], {
            "provider": "anthropic",
            "server": "gbstack_pi-rpc",
            "thinking": None,
            "waitMs": None,
            "fallbackToNativeOnTransientFailure": False,
            "rework": None,
            "fallback": None,
        })

        full = self.success(*options, "role", "mcp-full")
        self.assertEqual(full["effectiveProvider"], "openai")
        self.assertFalse(full["requiresCliPreflight"])
        self.assertEqual(full["mcp"]["server"], "custom_pi_rpc")
        self.assertEqual(full["mcp"]["model"], "gpt-5.4")
        self.assertEqual(full["mcp"]["thinking"], "high")
        self.assertEqual(full["mcp"]["waitMs"], 30000)
        self.assertTrue(full["mcp"]["fallbackToNativeOnTransientFailure"])
        self.assertEqual(full["mcp"]["rework"]["thinking"], "max")
        self.assertEqual(full["mcp"]["rework"]["waitMs"], 60000)
        self.assertEqual(full["mcp"]["rework"]["server"], "custom_pi_rpc")
        self.assertEqual(full["mcp"]["fallback"]["model"], "gpt-5.3")
        self.assertEqual(full["mcp"]["fallback"]["thinking"], "medium")

        cli_ids = [entry["id"] for entry in self.success(*options, "cli-roles")]
        self.assertNotIn("mcp-minimal", cli_ids)
        self.assertNotIn("mcp-full", cli_ids)

    def test_mcp_validation_errors(self):
        user_cases = [
            (self.role("worker", mode="mcp", mcp=None), "$.roles.worker.mcp"),
            (self.role("worker", mode="mcp", mcp={"provider": "openai", "unknown": True}), "$.roles.worker.mcp.unknown"),
            (self.role("worker", mode="mcp", mcp={}), "$.roles.worker.mcp.provider"),
            (self.role("worker", mode="mcp", mcp={"provider": ""}), "$.roles.worker.mcp.provider"),
            (self.role("worker", mode="mcp", mcp={"provider": "openai", "server": ""}), "$.roles.worker.mcp.server"),
            (self.role("worker", mode="mcp", mcp={"provider": "openai", "model": ""}), "$.roles.worker.mcp.model"),
            (self.role("worker", mode="mcp", mcp={"provider": "openai", "thinking": "ultra"}), "$.roles.worker.mcp.thinking"),
            (self.role("worker", mode="mcp", mcp={"provider": "openai", "waitMs": 0}), "$.roles.worker.mcp.waitMs"),
            (self.role("worker", mode="mcp", mcp={"provider": "openai", "waitMs": -1}), "$.roles.worker.mcp.waitMs"),
            (self.role("worker", mode="mcp", mcp={"provider": "openai", "waitMs": True}), "$.roles.worker.mcp.waitMs"),
            (self.role("worker", mode="mcp", mcp={"provider": "openai", "outerModel": ""}), "$.roles.worker.mcp.outerModel"),
            (
                self.role("worker", mode="mcp", native=None, cli=None, mcp={
                    "provider": "openai", "fallbackToNativeOnTransientFailure": True,
                }),
                "$.roles.worker.mcp.fallbackToNativeOnTransientFailure",
            ),
            (
                self.role("worker", mode="mcp", mcp={
                    "provider": "openai", "rework": {"unknown": 1},
                }),
                "$.roles.worker.mcp.rework.unknown",
            ),
            (
                self.role("worker", mode="mcp", mcp={
                    "provider": "openai", "rework": {"thinking": "invalid"},
                }),
                "$.roles.worker.mcp.rework.thinking",
            ),
            (
                self.role("worker", mode="mcp", mcp={
                    "provider": "openai", "fallback": {"waitMs": 0},
                }),
                "$.roles.worker.mcp.fallback.waitMs",
            ),
            (
                self.role("worker", mode="mcp", mcp={
                    "provider": "openai", "fallback": {"provider": None},
                }),
                "$.roles.worker.mcp.fallback.provider",
            ),
            (
                self.role("worker", mode="mcp", mcp={
                    "provider": "openai", "rework": {"fallbackToNativeOnTransientFailure": True},
                }),
                "$.roles.worker.mcp.rework.fallbackToNativeOnTransientFailure",
            ),
        ]
        for config, path in user_cases:
            with self.subTest(path=path, config=config):
                self.invalid_user(config, path)

    def test_mcp_review_distinct_provider_validation(self):
        self.config(self.user_config, {
            "roles": {
                "reviewer-mcp": dict(custom_mcp_role(provider="deepseek"), readonly=True),
            },
            "reviews": {
                "council": {
                    "members": ["reviewer-mcp", "reviewer-gemini", "reviewer-claude"],
                    "minDistinctProviders": 3,
                    "requireDistinctProviders": True,
                },
            },
        })
        review = self.success("--user-config-dir", self.user_dir, "review", "council")
        self.assertEqual(review["distinctProviders"], ["deepseek", "google", "anthropic"])
        self.assertFalse(review["requiresCliPreflight"])
        self.assertEqual(review["cliRoles"], [])

        # Duplicate provider with reviewer-gemini ("google") fails distinct provider check
        self.config(self.user_config, {
            "roles": {
                "reviewer-mcp": dict(custom_mcp_role(provider="google"), readonly=True),
            },
            "reviews": {
                "council": {
                    "members": ["reviewer-mcp", "reviewer-gemini", "reviewer-claude"],
                    "minDistinctProviders": 3,
                    "requireDistinctProviders": True,
                },
            },
        })
        self.failure(
            self.invoke("--user-config-dir", self.user_dir, "validate"),
            1, "user", self.user_config, "$.reviews.council.minDistinctProviders",
        )

    def test_review_mcp_rework_and_fallback_provider_validation(self):
        for variant in ("rework", "fallback"):
            for provider, allow, distinct, minimum, ok in [
                ("deepseek", False, True, 2, True), ("other", False, True, 2, True),
                ("google", False, True, 2, False), ("google", True, True, 2, variant == "fallback"),
                ("google", False, False, 2, True),
                ("google", True, True, 3, False), ("other", True, True, 3, True),
            ]:
                with self.subTest(variant=variant, provider=provider, allow=allow, distinct=distinct, minimum=minimum):
                    self.config(self.user_config, {
                        "roles": {
                            "reviewer-mcp": dict(
                                custom_mcp_role(provider="deepseek", **{variant: {"provider": provider}}),
                                readonly=True,
                            ),
                        },
                        "reviews": {"council": {
                            "members": ["reviewer-mcp", "reviewer-gemini", "reviewer-claude"],
                            "minDistinctProviders": minimum, "requireDistinctProviders": distinct,
                            "allowSameProviderFallback": allow,
                        }},
                    })
                    result = self.invoke("--user-config-dir", self.user_dir, "validate")
                    if ok:
                        self.assertEqual(result.returncode, 0, result.stderr)
                    else:
                        self.failure(
                            result, 1, self.user_config,
                            f"$.roles.reviewer-mcp.mcp.{variant}.provider", "council",
                        )

        # Mixed CLI + MCP members whose variants collapse to the same provider are rejected
        self.invalid_user({
            "roles": {
                "reviewer-mcp": dict(
                    custom_mcp_role(provider="a", rework={"provider": "c"}),
                    readonly=True,
                ),
                "reviewer-cli": dict(
                    custom_cli_role(provider="b", fallback={"provider": "c"}),
                    readonly=True,
                ),
            },
            "reviews": {"custom": {
                "members": ["reviewer-mcp", "reviewer-cli"],
                "minDistinctProviders": 2,
                "requireDistinctProviders": True,
                "allowSameProviderFallback": False,
            }},
        }, "$.roles.reviewer-mcp.mcp.rework.provider", "custom")

    def test_cross_layer_mcp_mode_flip_origin_attribution(self):
        # Project layer flips scout to mode=mcp without mcp config -> blames project
        self.invalid(self.role("scout", mode="mcp"), "$.roles.scout.mcp")
        # User layer pre-configures mcp rework conflicting with reviewer-gemini (inactive while mode=cli);
        # project layer flips mode=mcp -> blames project
        self.config(self.user_config, self.role("reviewer-codex", mcp={
            "provider": "deepseek",
            "rework": {"provider": "google"},
        }))
        self.config(self.project_config, self.role("reviewer-codex", mode="mcp"))
        result = self.invoke("--user-config-dir", self.user_dir, "--project-config-dir", self.project_dir, "validate")
        self.failure(result, 1, "project", self.project_config, "$.roles.reviewer-codex.mcp.rework.provider", "council")

    def test_project_mcp_security_boundary(self):
        for bad in (
            self.role("custom-mcp", **custom_mcp_role()),
            self.role("worker", mcp={"provider": "openai"}),
            self.role("worker", mcp={"rework": {"thinking": "high"}}),
        ):
            with self.subTest(bad=bad):
                self.invalid(bad, "--allow-project-cli")

        # Project layer MAY set mcp: null with mode: native
        self.config(self.project_config, {
            "roles": {"worker": {"mode": "native", "mcp": None}},
        })
        worker_native = self.success("--project-config-dir", self.project_dir, "role", "worker")
        self.assertEqual(worker_native["mode"], "native")
        self.assertIsNone(worker_native["mcp"])

        # Project layer MAY define/override mcp when --allow-project-cli or GBSTACK_ALLOW_PROJECT_CLI=1 is set
        self.config(self.project_config, {
            "roles": {"mcp-worker": custom_mcp_role(provider="anthropic", thinking="medium")},
        })
        mcp_flag = self.success("--project-config-dir", self.project_dir, "--allow-project-cli", "role", "mcp-worker")
        self.assertEqual(mcp_flag["mcp"]["thinking"], "medium")
        mcp_env = self.success(
            "--project-config-dir", self.project_dir, "role", "mcp-worker",
            env=dict(self.env, GBSTACK_ALLOW_PROJECT_CLI="1"),
        )
        self.assertEqual(mcp_env["mcp"]["thinking"], "medium")


if __name__ == "__main__":
    unittest.main()
