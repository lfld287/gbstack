import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BUILTINS = ("worker-codex", "worker-codex-xhigh", "reviewer-codex")


class CodexScriptsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base / "home"
        self.codex_home = self.base / "codex home"
        self.user_config = self.base / "user config"
        self.project = self.base / "project"
        self.project_config = self.project / ".agents" / "gbstack"
        self.bin = self.base / "bin"
        self.login_log = self.base / "login.log"
        for directory in (self.home, self.project, self.bin):
            directory.mkdir()
        # Only these tools and the test stub can be found on PATH.
        for name in ("cat", "chmod", "dirname", "link", "mkdir", "mktemp", "perl", "rm"):
            executable = shutil.which(name)
            self.assertIsNotNone(executable, f"{name} is required by the test fixture")
            (self.bin / name).symlink_to(executable)
        (self.bin / "bash").symlink_to("/bin/bash")
        (self.bin / "python3").symlink_to(sys.executable)
        self.codex = self.write(
            self.bin / "codex",
            "#!/bin/bash\n"
            'printf "%s\\n" "$*" "$CODEX_HOME" "$TMPDIR" > "$LOGIN_LOG"\n'
            'printf "%s\\n" "${LOGIN_OUTPUT-Logged in via test stub}"\n'
            'exit "${LOGIN_EXIT-0}"\n',
        )
        self.codex.chmod(0o755)
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex_home),
            "GBSTACK_USER_CONFIG_DIR": str(self.user_config),
            "GBSTACK_PROJECT_CONFIG_DIR": str(self.project_config),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LOGIN_LOG": str(self.login_log),
        }

    def write(self, path, contents):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def config(self, directory, **roles):
        return self.write(directory / "roles.json", json.dumps({"roles": roles}))

    def set_native_roles(self, **roles):
        self.config(self.project_config, **{
            "worker": {"mode": "native"},
            "reviewer-codex": {
                "mode": "native",
                "native": {"provider": "test", "model": "test-model"},
            },
            **roles,
        })

    def profile_path(self, name):
        return self.codex_home / f"{name}.config.toml"

    def seed_profiles(self, *profiles):
        for name in profiles:
            self.write(self.profile_path(name), 'model = "test"\n')

    def invoke(self, script, *args, env=None, cwd=None):
        return subprocess.run(
            [str(ROOT / "scripts" / f"{script}-codex.sh"), *map(str, args)],
            cwd=self.base if cwd is None else cwd,
            env=self.env if env is None else env,
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_exit(self, result, code):
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))

    def assert_checked(self, result, *profiles):
        self.assert_exit(result, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.splitlines(), [
            "Logged in via test stub", *(f"Readable profile: {self.profile_path(name)}" for name in profiles),
        ])

    def test_check_defaults_and_login_environment(self):
        self.seed_profiles(*BUILTINS)
        self.assert_checked(self.invoke("check", "--codex-home", self.codex_home), *BUILTINS)
        self.assertEqual(self.login_log.read_text().splitlines(), [
            "login status", str(self.codex_home), str(self.codex_home),
        ])

    def test_check_and_setup_dry_run_with_read_only_cwd_and_tmpdir(self):
        read_only = self.base / "read-only"
        read_only.mkdir(mode=0o500)
        self.addCleanup(read_only.chmod, 0o700)
        env = dict(self.env, TMPDIR=str(read_only))
        self.seed_profiles(*BUILTINS)
        self.assert_checked(self.invoke("check", env=env, cwd=read_only), *BUILTINS)

        dry_run_home = read_only / "codex"
        result = self.invoke("setup", "--dry-run", "--codex-home", dry_run_home, env=env, cwd=read_only)
        self.assert_exit(result, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.splitlines(), [
            f"Would create directory: {dry_run_home}",
            *(f"Would install profile: {dry_run_home / self.profile_path(name).name}" for name in BUILTINS),
        ])
        self.assertEqual(list(read_only.iterdir()), [])

    def test_check_reports_missing_profile(self):
        self.seed_profiles(*BUILTINS[:-1])
        result = self.invoke("check")
        self.assert_exit(result, 1)
        self.assertIn(f"profile is not a readable regular file: {self.profile_path(BUILTINS[-1])}", result.stderr)

    def test_check_respects_config_selection_flags(self):
        self.config(self.project_config, worker={"codex": {"profile": "project-worker"}})
        user_override = self.base / "alternate user"
        self.config(user_override, worker={"codex": {"extraProfiles": ["user-extra"]}})
        self.seed_profiles(*BUILTINS, "project-worker", "user-extra")
        discovery_env = self.env.copy()
        del discovery_env["GBSTACK_PROJECT_CONFIG_DIR"]
        for args, env, profiles in (
            (("--cwd", self.project), discovery_env, ("project-worker", *BUILTINS[1:])),
            (("--project-config-dir", self.project_config, "--user-config-dir", user_override),
             discovery_env, ("project-worker", "user-extra", BUILTINS[-1])),
            (("--user-config-dir", user_override, "--no-user-config"),
             self.env, ("project-worker", *BUILTINS[1:])),
            (("--user-config-dir", user_override, "--no-project-config"),
             self.env, (BUILTINS[0], "user-extra", BUILTINS[-1])),
        ):
            with self.subTest(args=args):
                self.assert_checked(self.invoke("check", *args, env=env), *profiles)

    def test_check_skips_when_codex_roles_are_disabled(self):
        self.set_native_roles()
        self.codex.unlink()
        result = self.invoke("check")
        self.assert_exit(result, 0)
        self.assertEqual(result.stdout, "No Codex profiles configured in active roles; skipping Codex check.\n")

    def test_scripts_report_corrupted_roles(self):
        config = self.write(self.project_config / "roles.json", "{")
        self.seed_profiles(*BUILTINS)
        for script in ("check", "setup"):
            with self.subTest(script=script):
                result = self.invoke(script)
                self.assert_exit(result, 1)
                self.assertIn(str(config), result.stderr)
                self.assertEqual(result.stdout, "")
        self.assertFalse(self.login_log.exists())

    def test_check_explicit_profiles_bypass_role_resolution(self):
        self.seed_profiles("manual-profile", BUILTINS[0])
        self.set_native_roles()
        self.assert_checked(self.invoke("check", "manual-profile", BUILTINS[0]), "manual-profile", BUILTINS[0])
        self.write(self.project_config / "roles.json", "{")
        self.assert_checked(self.invoke("check", "manual-profile"), "manual-profile")

    def test_check_reports_missing_codex(self):
        self.codex.unlink()
        result = self.invoke("check")
        self.assert_exit(result, 127)
        self.assertIn("no executable codex found", result.stderr)

    def test_check_reports_login_failure(self):
        self.seed_profiles(*BUILTINS)
        for env, message in (
            ({"LOGIN_EXIT": "9", "LOGIN_OUTPUT": "auth failure"}, "exited 9: auth failure"),
            ({"LOGIN_OUTPUT": "Not logged in"}, 'did not report "Logged in": Not logged in'),
        ):
            with self.subTest(env=env):
                result = self.invoke("check", env=dict(self.env, **env))
                self.assert_exit(result, 1)
                self.assertIn(message, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_scripts_reject_invalid_arguments(self):
        for script in ("check", "setup"):
            for args in (
                ("--unknown",), ("bad/profile",), ("",), ("--codex-home",),
                ("--cwd",), ("--user-config-dir", "--no-user-config"), ("--project-config-dir", ""),
            ):
                with self.subTest(script=script, args=args):
                    result = self.invoke(script, *args)
                    self.assert_exit(result, 2)
                    self.assertTrue(result.stderr)

    def test_setup_default_install_dry_run_skip_and_atomic_force(self):
        result = self.invoke("setup", "--dry-run", "--codex-home", self.codex_home)
        self.assert_exit(result, 0)
        self.assertEqual(result.stdout.splitlines(), [
            f"Would create directory: {self.codex_home}",
            *(f"Would install profile: {self.profile_path(name)}" for name in BUILTINS),
        ])
        self.assertFalse(self.codex_home.exists())

        result = self.invoke("setup")
        self.assert_exit(result, 0)
        self.assertEqual(set(self.codex_home.iterdir()), {self.profile_path(name) for name in BUILTINS})
        for name in BUILTINS:
            target = self.profile_path(name)
            self.assertEqual(target.read_bytes(), (ROOT / "templates" / "codex" / target.name).read_bytes())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

        target = self.write(self.profile_path(BUILTINS[0]), 'model = "local"\n')
        target.chmod(0o600)
        result = self.invoke("setup")
        self.assert_exit(result, 0)
        self.assertIn(f"Skipping existing profile: {target}", result.stdout)
        self.assertEqual(target.read_text(), 'model = "local"\n')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

        with target.open("rb") as original:
            result = self.invoke("setup", "--force", "--dry-run")
            self.assert_exit(result, 0)
            self.assertIn(f"Would replace profile: {target}", result.stdout)
            self.assertEqual(target.read_bytes(), b'model = "local"\n')

            result = self.invoke("setup", "--force")
            self.assert_exit(result, 0)
            self.assertEqual(original.read(), b'model = "local"\n')
            self.assertEqual(target.read_bytes(), (ROOT / "templates" / "codex" / target.name).read_bytes())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertEqual(set(self.codex_home.iterdir()), {self.profile_path(name) for name in BUILTINS})

    def test_setup_prefers_project_templates_for_custom_and_builtin_profiles(self):
        custom = "project-extra"
        project_override = self.base / "alternate project config"
        user_override = self.base / "alternate user config"
        self.config(project_override, worker={"codex": {"extraProfiles": [custom]}})
        for name in (BUILTINS[0], custom):
            relative = Path("templates/codex") / self.profile_path(name).name
            self.write(user_override / relative, 'model = "from-user"\n')
            self.write(project_override / relative, 'model = "from-project"\n')

        result = self.invoke("setup", "--user-config-dir", user_override, "--project-config-dir", project_override)
        self.assert_exit(result, 0)
        self.assertEqual(self.profile_path(BUILTINS[0]).read_text(), 'model = "from-project"\n')
        self.assertEqual(self.profile_path(custom).read_text(), 'model = "from-project"\n')
        self.assertFalse(self.profile_path(BUILTINS[1]).exists())

    def test_setup_skips_missing_templates_automatically_and_rejects_explicit_selection(self):
        custom = "without-template"
        self.config(self.project_config, worker={"codex": {"profile": custom}})
        for selected in ((custom, BUILTINS[1]), (BUILTINS[0], custom)):
            with self.subTest(selected=selected):
                result = self.invoke("setup", *selected)
                self.assert_exit(result, 1)
                self.assertIn(f"no template for profile: {custom}", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(self.codex_home.exists())

        result = self.invoke("setup")
        self.assert_exit(result, 0)
        self.assertIn(f"Skipping custom profile without template: {custom} (expected at {self.profile_path(custom)})",
                      result.stdout.splitlines())
        self.assertEqual(set(self.codex_home.iterdir()), {self.profile_path(name) for name in BUILTINS[1:]})

    def test_setup_only_custom_profile_without_template_leaves_home_absent(self):
        custom = "without-template"
        self.set_native_roles(worker={"mode": "codex", "codex": {"profile": custom, "extraProfiles": []}})
        result = self.invoke("setup", "--dry-run", "--force")
        self.assert_exit(result, 0)
        self.assertEqual(result.stdout.splitlines(), [
            f"Skipping custom profile without template: {custom} (expected at {self.profile_path(custom)})",
        ])
        self.assertFalse(self.codex_home.exists())

    def test_setup_only_installs_explicit_profiles(self):
        self.set_native_roles()
        selected = (BUILTINS[2], BUILTINS[0])
        result = self.invoke("setup", *selected)
        self.assert_exit(result, 0)
        self.assertEqual(set(self.codex_home.iterdir()), {self.profile_path(name) for name in selected})

    def test_setup_leaves_home_absent_when_codex_roles_are_disabled(self):
        self.set_native_roles()
        result = self.invoke("setup")
        self.assert_exit(result, 0)
        self.assertEqual(result.stdout, "No Codex profiles configured in active roles; nothing to install.\n")
        self.assertFalse(self.codex_home.exists())
