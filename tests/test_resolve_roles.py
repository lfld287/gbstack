import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "resolve-roles.sh"
PROFILES = ["worker-codex", "worker-codex-xhigh", "reviewer-codex"]


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

    def invalid(self, contents, path):
        self.failure(self.override(contents, "validate"), 1, "project", self.project_config, path)

    def role(self, role_id, **fields):
        return {
            "roles": {role_id: fields},
        }

    def test_builtin_commands_and_expanded_roles(self):
        result = self.success("validate")
        self.assertEqual(result, {
            "ok": True,
            "sources": {
                "default": str(ROOT / "config" / "roles.default.json"),
                "user": None,
                "project": None,
            },
            "roleCount": 8,
            "reviewCount": 3,
            "codexProfileCount": 3,
        })
        dump = self.success("dump")
        self.assertEqual(dump["schemaVersion"], 1)
        self.assertEqual(dump["pluginRoot"], str(ROOT))
        self.assertEqual(dump["sources"], result["sources"])
        self.assertEqual(list(dump["roles"]), [
            "coordinator", "scout", "plan", "worker", "reviewer-gemini",
            "reviewer-claude", "reviewer-codex", "monitor",
        ])
        self.assertEqual(dump["slots"], {
            "coordinator": "coordinator", "recon": "scout", "plan": "plan",
            "implement": "worker", "monitor": "monitor", "review": "default",
            "interrogate": "default", "codexReview": "codex", "council": "council",
        })
        worker = self.success("role", "worker")
        self.assertEqual(worker, dump["roles"]["worker"])
        self.assertEqual(worker["id"], "worker")
        self.assertEqual(worker["effectiveProvider"], "openai")
        self.assertTrue(worker["requiresCodexPreflight"])
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
        self.assertFalse(coordinator["requiresCodexPreflight"])
        self.assertEqual(dump["roles"]["monitor"]["skill"], "swarm_monitor")
        for name, providers, profiles in [
            ("default", ["google", "anthropic"], []),
            ("codex", ["openai", "google"], ["reviewer-codex"]),
            ("council", ["openai", "google", "anthropic"], ["reviewer-codex"]),
        ]:
            with self.subTest(review=name):
                review = self.success("review", name)
                self.assertEqual(review["id"], name)
                self.assertEqual(review["distinctProviders"], providers)
                self.assertEqual(review["requiresCodexPreflight"], bool(profiles))
                self.assertEqual(review["codexProfiles"], profiles)
                self.assertEqual(review["memberRoles"], [dump["roles"][member] for member in review["members"]])
        council = self.success("review", "council")
        self.assertEqual(self.success("slot", "council"), {
            "slot": "council", "kind": "review", "target": "council", "review": council,
        })
        self.assertEqual(council["maxRounds"], 3)

    def test_null_rework_model_uses_native_model(self):
        self.config(self.project_config, self.role("worker", native={"reworkModel": None}))
        worker = self.success("--project-config-dir", self.project_dir, "role", "worker")
        self.assertEqual(worker["native"]["reworkModel"], worker["native"]["model"])

    def test_config_merge_and_asset_priority(self):
        user_prompt = self.write(self.user_dir / "agents" / "worker.md", "user")
        project_prompt = self.write(self.project_dir / "agents" / "worker.md", "project")
        self.config(self.user_config, {
            "roles": {"worker": {"native": {"model": "user-model"}, "codex": {"extraProfiles": ["user-extra"]}}},
            "reviews": {"default": {"members": ["reviewer-claude", "reviewer-gemini"]}},
        })
        self.config(self.project_config, {
            "roles": {"worker": {"native": {"lowQuotaFallback": None}, "codex": {"extraProfiles": []}}},
            "reviews": {"default": {"members": ["reviewer-gemini"], "minDistinctProviders": 1}},
        })
        options = ("--user-config-dir", self.user_dir, "--project-config-dir", self.project_dir)
        result = self.success(*options, "dump")
        worker = result["roles"]["worker"]
        self.assertEqual(worker["native"]["model"], "user-model")
        self.assertEqual(worker["native"]["provider"], "google")
        self.assertIsNone(worker["native"]["lowQuotaFallback"])
        self.assertEqual(worker["codex"]["extraProfiles"], [])
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
                "native": {"provider": "test", "model": "fresh"}, "codex": None,
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
        self.assertIsNone(worker["codex"])
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
        self.assertEqual(self.success("--default-config", default, "validate",
                                      env=dict(alternate_env, GBSTACK_DEFAULT_CONFIG="missing"))["sources"]["default"],
                         str(default))

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

    def test_project_probe_prompt_only_or_template_only(self):
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
        template = self.write(nearest / "templates" / "codex" / "worker-codex.config.toml", "")
        profiles = self.success("--cwd", nested, "codex-profiles", "worker-codex", env=env)
        self.assertEqual(profiles[0]["templatePath"], str(template))
        self.assertEqual(profiles[0]["templateSource"], "project")

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

    def test_profiles_order_dedup_formats_and_missing_templates(self):
        profiles = self.success("codex-profiles")
        self.assertEqual([entry["profile"] for entry in profiles], PROFILES)
        for entry, sandbox, role in zip(profiles, ["workspace-write"] * 2 + ["read-only"], ["worker"] * 2 + ["reviewer-codex"]):
            self.assertEqual(entry, {
                "profile": entry["profile"], "sandbox": sandbox, "roles": [role],
                "templatePath": str(ROOT / "templates" / "codex" / (entry["profile"] + ".config.toml")),
                "templateSource": "default",
            })
        lines = self.invoke("codex-profiles", "--format", "lines")
        self.assertEqual((lines.returncode, lines.stdout, lines.stderr), (0, "\n".join(PROFILES) + "\n", ""))
        user_template = self.write(self.user_dir / "templates" / "codex" / "Custom.v2.config.toml", "user")
        project_template = self.write(self.project_dir / "templates" / "codex" / "Custom.v2.config.toml", "project")
        self.config(self.project_config, {
            "roles": {
                "worker": {"codex": {"extraProfiles": ["Custom.v2", "no-template"]}},
                "other-worker": {"mode": "codex", "readonly": False, "workspace": "inherit", "prompt": None,
                                 "codex": {"provider": "openai", "profile": "worker-codex", "sandbox": "workspace-write"}},
            },
        })
        options = ("--user-config-dir", self.user_dir, "--project-config-dir", self.project_dir)
        profiles = self.success(*options, "codex-profiles")
        self.assertEqual([entry["profile"] for entry in profiles], ["worker-codex", "Custom.v2", "no-template", "reviewer-codex"])
        self.assertEqual(profiles[0]["roles"], ["worker", "other-worker"])
        self.assertEqual(profiles[1]["templatePath"], str(project_template))
        self.assertEqual(profiles[1]["templateSource"], "project")
        self.assertIsNone(profiles[2]["templatePath"])
        self.assertIsNone(profiles[2]["templateSource"])
        project_template.unlink()
        explicit = self.success(*options, "codex-profiles", "unregistered", "Custom.v2", "worker-codex")
        self.assertEqual([entry["profile"] for entry in explicit], ["unregistered", "Custom.v2", "worker-codex"])
        self.assertIsNone(explicit[0]["templatePath"])
        self.assertIsNone(explicit[0]["templateSource"])
        self.assertEqual(explicit[1]["templatePath"], str(user_template))
        self.assertEqual(explicit[1]["templateSource"], "user")
        tsv = self.invoke(*options, "codex-profiles", "--format", "tsv", "Custom.v2", "no-template")
        self.assertEqual((tsv.returncode, tsv.stdout, tsv.stderr),
                         (0, "Custom.v2\t" + str(user_template) + "\tuser\nno-template\t\t\n", ""))

    def test_native_roles_do_not_register_inherited_codex_profiles(self):
        self.config(self.project_config, {"roles": {
            "worker": {"mode": "native"},
            "reviewer-codex": {"mode": "native", "native": {"provider": "test", "model": "test-model"}},
            "scout": {"codex": {}},
        }})
        self.assertEqual(self.success("--project-config-dir", self.project_dir, "codex-profiles"), [])

    def test_review_codex_profiles_exclude_extra_profiles(self):
        extra = "reviewer-extra"
        self.config(self.project_config, self.role("reviewer-codex", codex={"extraProfiles": [extra]}))
        options = ("--project-config-dir", self.project_dir)
        review = self.success(*options, "review", "council")
        self.assertEqual(review["codexProfiles"], ["reviewer-codex"])
        self.assertEqual([entry["profile"] for entry in self.success(*options, "codex-profiles")], [*PROFILES, extra])

    def test_tsv_rejects_control_characters_without_partial_output(self):
        for char in ["\t", "\n", "\r"]:
            with self.subTest(char=repr(char)):
                directory = self.base / ("project" + char)
                self.write(directory / "templates" / "codex" / "custom.config.toml", "")
                result = self.invoke("--project-config-dir", directory, "codex-profiles", "--format", "tsv",
                                     "worker-codex", "custom")
                self.failure(result, 1, "project", "custom.config.toml", "templatePath")

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
            ({"schemaVersion": 2}, "$.schemaVersion"),
            ({"schemaVersion": True}, "$.schemaVersion"),
            ({"unknown": 1}, "$.unknown"),
            ({"slots": {"unknown": None}}, "$.slots.unknown"),
            (self.role("worker", unknown=True), "$.roles.worker.unknown"),
            (self.role("worker", native={"unknown": True}), "$.roles.worker.native.unknown"),
            (self.role("worker", codex={"unknown": True}), "$.roles.worker.codex.unknown"),
            ({"reviews": {"default": {"unknown": True}}}, "$.reviews.default.unknown"),
            (self.role("worker", readonly=1), "$.roles.worker.readonly"),
            (self.role("worker", native=[]), "$.roles.worker.native"),
            (self.role("worker", codex={"extraProfiles": {}}), "$.roles.worker.codex.extraProfiles"),
        ]
        for config, path in cases:
            with self.subTest(path=path, config=config):
                self.invalid(config, path)
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

    def test_execution_mode_sandbox_and_profile_validation(self):
        cases = [
            (self.role("scout", native=None), "$.roles.scout.native"),
            (self.role("worker", codex=None), "$.roles.worker.codex"),
            (self.role("worker", codex={"profile": None}), "$.roles.worker.codex.profile"),
            (self.role("worker", native=None), "$.roles.worker.codex.fallbackToNativeOnPreflightFail"),
            (self.role("worker", native=None, codex={"fallbackToNativeOnPreflightFail": False}),
             "$.roles.worker.codex.fallbackToNativeOnTransientFailure"),
            (self.role("worker", codex={"sandbox": "danger-full-access"}), "$.roles.worker.codex.sandbox"),
            (self.role("worker", mode="native", codex={"sandbox": "invalid"}), "$.roles.worker.codex.sandbox"),
            (self.role("reviewer-codex", codex={"sandbox": "workspace-write"}), "$.roles.reviewer-codex.codex.sandbox"),
            (self.role("scout", codex={"sandbox": "workspace-write"}), "$.roles.scout.codex.sandbox"),
            (self.role("worker", codex={"profile": "bad/profile"}), "$.roles.worker.codex.profile"),
            (self.role("worker", codex={"extraProfiles": ["x" * 65]}), "$.roles.worker.codex.extraProfiles[0]"),
            (self.role("worker", codex={"extraProfiles": ["worker-codex"]}), "$.roles.worker.codex.extraProfiles[0]"),
            (self.role("worker", codex={"extraProfiles": ["same", "same"]}), "$.roles.worker.codex.extraProfiles[1]"),
            (self.role("reviewer-codex", codex={"profile": "worker-codex"}), "$.roles.reviewer-codex.codex.profile"),
            (self.role("reviewer-codex", codex={"extraProfiles": ["worker-codex-xhigh"]}),
             "$.roles.reviewer-codex.codex.extraProfiles[0]"),
        ]
        for config, path in cases:
            with self.subTest(config=config):
                self.invalid(config, path)

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

    def test_template_shadowing(self):
        leaf = self.project_dir / "templates" / "codex" / "worker-codex.config.toml"
        leaf.mkdir(parents=True)
        options = ("--project-config-dir", self.project_dir)
        self.failure(self.invoke(*options, "validate"), 1, "project", leaf, "worker-codex")
        leaf.rmdir()
        leaf.symlink_to(self.base / "missing")
        self.failure(self.invoke(*options, "codex-profiles", "worker-codex"), 1, "project", leaf, "worker-codex")
        leaf.unlink()
        self.write(leaf, "").chmod(0)
        self.failure(self.invoke(*options, "codex-profiles", "worker-codex"), 1, "project", leaf, "worker-codex")

    def test_cli_usage_and_missing_queries(self):
        for args in [(), ("unrecognized",), ("role",), ("slot", "plan", "extra"),
                     ("codex-profiles", "--format", "yaml"), ("codex-profiles", "bad/name")]:
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
            self.assertIn("codex-profiles", result.stdout)

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


if __name__ == "__main__":
    unittest.main()
