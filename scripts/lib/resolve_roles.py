"""Resolve GBStack roles, review groups, and CLI configurations from local config."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LAYERS = ("default", "user", "project")
ROLE_SLOTS = ("coordinator", "recon", "plan", "implement", "monitor")
REVIEW_SLOTS = ("review", "interrogate", "council")
ROLE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")
NATIVE_FALLBACK_FLAGS = ("fallbackToNativeOnPreflightFail", "fallbackToNativeOnTransientFailure")
CLI_VARIANTS = ("rework", "fallback")
CLI_BINARY_BOUND_FIELDS = (
    "fallbackBinaries", "args", "nonGitArgs", "extraDirArgs", "stdinArg", "promptPrefixArgs", "env", "preflight",
)
CLI_VARIANT_FIELDS = (
    "provider", "binary", "fallbackBinaries", "args", "nonGitArgs", "extraDirArgs",
    "promptInput", "stdinArg", "promptPrefixArgs", "env", "timeoutSeconds", "preflight",
    "outerModel", "outerLowQuotaFallback",
)
CLI_PREFLIGHT_FIELDS = ("command", "outputContains", "env", "timeoutSeconds")
MCP_DEFAULT_SERVER = "gbstack_pi-rpc"
MCP_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
MCP_VARIANT_FIELDS = (
    "provider", "server", "model", "thinking", "waitMs",
    "outerModel", "outerLowQuotaFallback",
)
FIELDS = {
    (): ("schemaVersion", "roles", "reviews", "slots"),
    ("slots",): ROLE_SLOTS + REVIEW_SLOTS,
    ("roles", "*"): (
        "description", "prompt", "skill", "readonly", "workspace", "concurrentWorkspace",
        "mode", "native", "cli", "mcp",
    ),
    ("roles", "*", "native"): ("provider", "model", "reworkModel", "lowQuotaFallback", "lowQuotaFallbackProvider"),
    ("roles", "*", "cli"): CLI_VARIANT_FIELDS + NATIVE_FALLBACK_FLAGS + CLI_VARIANTS,
    ("roles", "*", "cli", "preflight"): CLI_PREFLIGHT_FIELDS,
    **{("roles", "*", "cli", variant): CLI_VARIANT_FIELDS for variant in CLI_VARIANTS},
    **{("roles", "*", "cli", variant, "preflight"): CLI_PREFLIGHT_FIELDS for variant in CLI_VARIANTS},
    ("roles", "*", "mcp"): MCP_VARIANT_FIELDS + ("fallbackToNativeOnTransientFailure",) + CLI_VARIANTS,
    **{("roles", "*", "mcp", variant): MCP_VARIANT_FIELDS for variant in CLI_VARIANTS},
    ("reviews", "*"): (
        "description", "members", "requireDistinctProviders", "minDistinctProviders",
        "allowSameProviderFallback", "onMissingProvider", "onIncomplete", "maxRounds",
    ),
}


class ConfigError(Exception):
    def __init__(self, source, field, message):
        layer, path = source
        json_path = "$"
        for part in field:
            if isinstance(part, int):
                json_path += f"[{part}]"
            elif re.fullmatch(r"[a-zA-Z0-9_-]+", part):
                json_path += "." + part
            else:
                json_path += "[" + json.dumps(part) + "]"
        super().__init__(f"{layer}: {path}: {json_path}: {message}")


def readable_file(source, field):
    """Return false for an absent path. An existing invalid entry shadows lower layers."""
    _, path = source
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as error:
        raise ConfigError(source, field, str(error)) from error
    try:
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            raise ConfigError(source, field, "expected a regular file")
        if not mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH) or not os.access(path, os.R_OK):
            raise ConfigError(source, field, "file is unreadable")
    except (OSError, ValueError) as error:
        raise ConfigError(source, field, str(error)) from error
    return True


def reject_constant(value):
    raise ValueError(f"invalid JSON constant {value}")


def load_layer(source):
    layer, path = source
    if not readable_file(source, ()):
        if layer == "default":
            raise ConfigError(source, (), "required config file is missing")
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (ValueError, OSError, RecursionError) as error:
        raise ConfigError(source, (), str(error)) from error
    if not isinstance(document, dict):
        raise ConfigError(source, (), "expected a JSON object")
    if "schemaVersion" in document:
        version = document["schemaVersion"]
        if type(version) is int and version == 1:
            raise ConfigError(
                source,
                ("schemaVersion",),
                "schemaVersion 1 is no longer supported; migrate to schemaVersion 2 "
                "(replace mode 'codex' with 'cli', field 'codex' with 'cli', and remove slots.codexReview)",
            )
        if type(version) is not int or version != 2:
            raise ConfigError(source, ("schemaVersion",), "expected schema version 2")
    return document


def project_config_dir(cwd):
    path = Path(cwd)
    try:
        path = path.expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ConfigError(("project", path), (), "--cwd must be a directory")
        ancestors = (path, *path.parents)
        git_depth = next(
            (index for index, directory in enumerate(ancestors)
             if (directory / ".git").is_dir() or (directory / ".git").is_file()),
            0,
        )
        for directory in ancestors[:git_depth + 1]:
            candidate = directory / ".agents" / "gbstack"
            if any(os.path.lexists(candidate / item) for item in ("roles.json", "agents")):
                return candidate
    except (OSError, ValueError, RuntimeError) as error:
        raise ConfigError(("project", path), (), str(error)) from error
    return None


def config_path(layer, value):
    path = Path(value)
    try:
        return path.expanduser().absolute()
    except (OSError, ValueError, RuntimeError) as error:
        raise ConfigError((layer, path), (), str(error)) from error


def config_locations(args):
    default = args.default_config
    if default is None:
        default = os.environ.get("GBSTACK_DEFAULT_CONFIG", PLUGIN_ROOT / "config" / "roles.default.json")
    user = args.user_config_dir
    if user is None:
        user = os.environ.get("GBSTACK_USER_CONFIG_DIR", "~/.gemini/config/gbstack")
    project = args.project_config_dir
    if project is None:
        project = os.environ.get("GBSTACK_PROJECT_CONFIG_DIR")
    directories = dict.fromkeys(LAYERS)
    directories["default"] = PLUGIN_ROOT
    for layer, value, disabled in [
        ("user", user, args.no_user_config), ("project", project, args.no_project_config),
    ]:
        if disabled:
            continue
        if value:
            directories[layer] = config_path(layer, value)
        elif value is None:
            directories[layer] = project_config_dir(args.cwd)
    return config_path("default", default), directories


def select_cli_profile(cli_cfg, profile="default") -> tuple[dict | None, str | None]:
    """Return the selected CLI configuration and its effective profile name."""
    if profile == "default":
        return cli_cfg, "default"
    if profile in CLI_VARIANTS:
        variant = cli_cfg.get(profile)
        if variant is not None:
            return variant, profile
        if profile == "rework":
            return cli_cfg, "default"
    return None, None


class Resolver:
    def __init__(self, args):
        self.allow_project_cli = bool(
            getattr(args, "allow_project_cli", False)
            or os.environ.get("GBSTACK_ALLOW_PROJECT_CLI") == "1"
        )
        default_file, self.directories = config_locations(args)
        self.default_source = ("default", default_file)
        self.config = {}
        self.origins = {}
        self.sources = dict.fromkeys(self.directories)
        for layer, directory in self.directories.items():
            if directory is None:
                continue
            path = default_file if layer == "default" else directory / "roles.json"
            source = (layer, path)
            document = load_layer(source)
            if document is not None:
                self.merge(self.config, document, source, ())
                self.sources[layer] = str(path)

        self.expect(self.config, (), "schemaVersion", int)
        role_registry = self.expect(self.config, (), "roles", dict)
        review_registry = self.expect(self.config, (), "reviews", dict)
        self.slots = self.expect(self.config, (), "slots", dict)
        self.roles = {
            role_id: self.resolve_role(role_id, role)
            for role_id, role in role_registry.items() if role is not None
        }
        self.reviews = {
            review_id: self.resolve_review(review_id, review)
            for review_id, review in review_registry.items() if review is not None
        }
        self.validate_slots()

    def merge(self, base, update, source, path):
        shape = path
        if len(path) > 1 and path[0] in ("roles", "reviews"):
            shape = (path[0], "*", *path[2:])
        allowed = FIELDS.get(shape)
        merging_cli = shape == ("roles", "*", "cli")
        merging_variant = len(shape) == 4 and shape[:3] == ("roles", "*", "cli") and shape[3] in CLI_VARIANTS
        if merging_cli:
            # Use the previous layer's inherited values regardless of the order of keys in this update.
            base_binary = base.get("binary")
            base_prompt_input = base.get("promptInput")

        def _clean_origins(target_path):
            for origin_path in list(self.origins):
                if origin_path[:len(target_path)] == target_path:
                    del self.origins[origin_path]

        for key, new in update.items():
            field = (*path, key)
            if path == ("slots",) and key == "codexReview":
                raise ConfigError(
                    source,
                    field,
                    "slots.codexReview was removed in schemaVersion 2; migrate to schemaVersion 2 and remove slots.codexReview",
                )
            if len(path) == 2 and path[0] == "roles" and key == "codex":
                raise ConfigError(
                    source,
                    field,
                    "role field 'codex' was replaced by 'cli' in schemaVersion 2; migrate 'codex' to 'cli'",
                )
            if allowed is not None and key not in allowed:
                raise ConfigError(source, field, "unknown key")
            if (
                source[0] == "project"
                and not self.allow_project_cli
                and len(path) == 2
                and path[0] == "roles"
                and key in ("cli", "mcp")
                and new is not None
            ):
                raise ConfigError(
                    source,
                    field,
                    f"project config cannot define or override '{key}' without --allow-project-cli",
                )
            if path == ("roles",) and (not ROLE_ID.fullmatch(key) or key == "all" or key.startswith("coordinator-")):
                raise ConfigError(source, field, "invalid or reserved role ID")
            old = base.get(key)
            if old is not None and new is not None and type(old) is not type(new):
                raise ConfigError(source, field, f"type conflict ({type(old).__name__} vs. {type(new).__name__})")
            if isinstance(old, dict):
                if not isinstance(new, dict):
                    _clean_origins(field)
                elif merging_cli and key in CLI_VARIANTS:
                    prev_binary = old.get("binary") if old.get("binary") is not None else base_binary
                    new_binary = new.get("binary")
                    if new_binary is not None and prev_binary is not None and new_binary != prev_binary:
                        for name in CLI_BINARY_BOUND_FIELDS:
                            if name in old:
                                del old[name]
                                _clean_origins((*field, name))

                    prev_input = old.get("promptInput") if old.get("promptInput") is not None else base_prompt_input
                    new_input = new.get("promptInput")
                    if new_input in ("stdin", "arg") and new_input != prev_input:
                        incompatible = "stdinArg" if new_input == "arg" else "promptPrefixArgs"
                        if incompatible in old and incompatible not in new:
                            del old[incompatible]
                            _clean_origins((*field, incompatible))
                elif merging_variant and key == "preflight":
                    old = {}
                    _clean_origins(field)
            self.origins[field] = source
            if isinstance(new, dict):
                base[key] = {} if old is None else old
                self.merge(base[key], new, source, field)
            else:
                base[key] = new

    def origin(self, path):
        for length in range(len(path), 0, -1):
            if path[:length] in self.origins:
                return self.origins[path[:length]]
        return self.default_source

    def invalid(self, path, message, source=None):
        raise ConfigError(source or self.origin(path), path, message)

    def latest_source(self, *paths):
        return max((self.origin(path) for path in paths), key=lambda source: LAYERS.index(source[0]))

    def expect(self, node, path, key, kind, optional=False, extra_origins=()):
        value = node.get(key)
        if value is None and optional:
            return None
        if type(value) is not kind or (kind is str and not value):
            field = (*path, key)
            source = self.latest_source(field, *extra_origins) if extra_origins else None
            self.invalid(field, f"expected {'nonempty ' if kind is str else ''}{kind.__name__}", source)
        return value

    def expect_positive_int(self, node, path, key, default):
        value = node.get(key)
        if value is None:
            return default
        if type(value) is not int or value < 1:
            self.invalid((*path, key), "expected integer >= 1")
        return value

    def expect_str_list(self, node, path, key, nonempty_items=False, required=False, nonempty_list=False):
        value = node.get(key)
        if value is None:
            if required:
                self.invalid((*path, key), "expected nonempty list")
            return []
        if type(value) is not list:
            self.invalid((*path, key), "expected list")
        if nonempty_list and not value:
            self.invalid((*path, key), "expected nonempty list")
        for index, item in enumerate(value):
            if type(item) is not str or (nonempty_items and not item):
                self.invalid((*path, key, index), f"expected {'nonempty ' if nonempty_items else ''}str")
        return list(value)

    def expect_env_dict(self, node, path, key):
        value = node.get(key)
        if value is None:
            return {}
        if type(value) is not dict:
            self.invalid((*path, key), "expected dict")
        for env_key, env_val in value.items():
            if type(env_key) is not str or not env_key:
                self.invalid((*path, key, env_key), "expected nonempty str env key")
            if type(env_val) is not str:
                self.invalid((*path, key, env_key), "expected str env value")
        return dict(value)

    def lookup_asset(self, relative, field):
        for layer in reversed(LAYERS):
            directory = self.directories[layer]
            if directory is not None:
                path = directory / relative
                if readable_file((layer, path), field):
                    return str(path), layer
        return None, None

    def resolve_prompt(self, prompt, field):
        if prompt is None:
            return None, None
        path = Path(prompt)
        if path.is_absolute() or prompt.startswith("~/"):
            try:
                path = path.expanduser()
            except (ValueError, RuntimeError) as error:
                self.invalid(field, str(error))
            if readable_file((self.origin(field)[0], path), field):
                return str(path), "absolute"
        else:
            if ".." in path.parts:
                self.invalid(field, "relative prompt paths cannot contain '..'")
            result = self.lookup_asset(Path("agents") / path, field)
            if result[0] is not None:
                return result
        self.invalid(field, f"prompt has no readable file: {prompt!r}")

    def resolve_role(self, role_id, role):
        path = ("roles", role_id)
        if not isinstance(role, dict):
            self.invalid(path, "expected a role object or null")
        for key in ("description", "prompt", "skill", "concurrentWorkspace"):
            self.expect(role, path, key, str, optional=True)
        self.expect(role, path, "readonly", bool)
        self.expect(role, path, "workspace", str)
        mode_field = (*path, "mode")
        mode = self.expect(role, path, "mode", str)
        if mode == "codex":
            self.invalid(mode_field, "mode 'codex' was replaced by 'cli' in schemaVersion 2; migrate mode to 'cli'")
        if mode not in ("native", "cli", "mcp"):
            self.invalid(mode_field, "expected 'native', 'cli', or 'mcp'")
        native = self.expect(role, path, "native", dict, optional=mode != "native", extra_origins=(mode_field,))
        cli_cfg = self.expect(role, path, "cli", dict, optional=mode != "cli", extra_origins=(mode_field,))
        mcp_cfg = self.expect(role, path, "mcp", dict, optional=mode != "mcp", extra_origins=(mode_field,))
        resolved = dict(role)
        if native is not None:
            native_path = (*path, "native")
            native_extra = (mode_field,) if mode == "native" else ()
            self.expect(native, native_path, "provider", str, extra_origins=native_extra)
            model = self.expect(native, native_path, "model", str, extra_origins=native_extra)
            for key in ("reworkModel", "lowQuotaFallback", "lowQuotaFallbackProvider"):
                self.expect(native, native_path, key, str, optional=True)
            resolved["native"] = dict(native)
            if resolved["native"].get("reworkModel") is None:
                resolved["native"]["reworkModel"] = model
        if cli_cfg is not None:
            resolved["cli"] = self.resolve_cli(role_id, cli_cfg, mode, native)
        if mcp_cfg is not None:
            resolved["mcp"] = self.resolve_mcp(role_id, mcp_cfg, mode, native)
        prompt = role.get("prompt", f"{role_id}.md")
        prompt_path, prompt_source = self.resolve_prompt(prompt, (*path, "prompt"))
        return dict(
            resolved,
            id=role_id,
            prompt=prompt,
            promptPath=prompt_path,
            promptSource=prompt_source,
            effectiveProvider=resolved[mode]["provider"],
            requiresCliPreflight=mode == "cli",
        )

    def resolve_cli(self, role_id, cli_cfg, mode, native):
        path = ("roles", role_id, "cli")
        normalized = self._normalize_cli_fields(cli_cfg, path, mode)
        for flag in NATIVE_FALLBACK_FLAGS:
            flag_value = self.expect(cli_cfg, path, flag, bool, optional=True)
            if flag_value and native is None:
                field = (*path, flag)
                self.invalid(
                    field,
                    "requires a native configuration",
                    self.latest_source(field, ("roles", role_id, "native")),
                )
            normalized[flag] = bool(flag_value)
        for variant in CLI_VARIANTS:
            override = self.expect(cli_cfg, path, variant, dict, optional=True)
            normalized[variant] = self._normalize_cli_profile(normalized, override, (*path, variant), mode)
        return normalized

    def _normalize_cli_profile(self, base, override, path, mode):
        if override is None:
            return None
        profile = {key: base[key] for key in CLI_VARIANT_FIELDS if key in base}
        if override.get("binary", base.get("binary")) != base.get("binary"):
            # Arguments, environment, and preflight belong to the executable, not the role.
            profile.update(dict.fromkeys(CLI_BINARY_BOUND_FIELDS))
        inherited_env = profile["env"] or {}
        profile.update(override)
        if "env" in override:
            env = self.expect_env_dict(override, path, "env")
            profile["env"] = {**inherited_env, **env} if override["env"] is not None else {}
        if profile["timeoutSeconds"] is None:
            profile["timeoutSeconds"] = base["timeoutSeconds"]
        if "promptInput" in override and profile["promptInput"] != base.get("promptInput"):
            if profile["promptInput"] == "arg" and "stdinArg" not in override:
                profile["stdinArg"] = None
            if profile["promptInput"] == "stdin" and "promptPrefixArgs" not in override:
                profile["promptPrefixArgs"] = []
        prompt_field = (*path, "promptInput") if "promptInput" in override else (*path[:-1], "promptInput")
        return self._normalize_cli_fields(profile, path, mode, prompt_field)

    def _normalize_cli_fields(self, cli_cfg, path, mode, prompt_field=None):
        optional = mode != "cli"
        mode_origins = ((*path[:2], "mode"),) if not optional else ()
        if prompt_field is None:
            prompt_field = (*path, "promptInput")
        self.expect(cli_cfg, path, "provider", str, optional=optional, extra_origins=mode_origins)
        self.expect(cli_cfg, path, "binary", str, optional=optional, extra_origins=mode_origins)
        fallback_binaries = self.expect_str_list(cli_cfg, path, "fallbackBinaries", nonempty_items=True)
        args = self.expect_str_list(cli_cfg, path, "args", nonempty_items=False)
        non_git_args = self.expect_str_list(cli_cfg, path, "nonGitArgs", nonempty_items=False)
        extra_dir_args = self.expect_str_list(cli_cfg, path, "extraDirArgs", nonempty_items=False)
        prompt_input = self.expect(cli_cfg, path, "promptInput", str, optional=optional, extra_origins=mode_origins)
        if prompt_input is not None and prompt_input not in ("stdin", "arg"):
            self.invalid((*path, "promptInput"), "expected 'stdin' or 'arg'")
        stdin_arg = self.expect(cli_cfg, path, "stdinArg", str, optional=True)
        prompt_prefix_args = self.expect_str_list(cli_cfg, path, "promptPrefixArgs", nonempty_items=False)
        if stdin_arg is not None and prompt_input == "arg":
            field = (*path, "stdinArg")
            self.invalid(
                field,
                "stdinArg cannot be set when promptInput is 'arg'",
                self.latest_source(field, prompt_field),
            )
        if prompt_prefix_args and prompt_input == "stdin":
            field = (*path, "promptPrefixArgs")
            self.invalid(
                field,
                "promptPrefixArgs cannot be non-empty when promptInput is 'stdin'",
                self.latest_source(field, prompt_field),
            )
        env = self.expect_env_dict(cli_cfg, path, "env")
        timeout_seconds = self.expect_positive_int(cli_cfg, path, "timeoutSeconds", default=600)
        preflight_raw = self.expect(cli_cfg, path, "preflight", dict, optional=True)
        preflight = None
        if preflight_raw is not None:
            pf_path = (*path, "preflight")
            command = self.expect_str_list(
                preflight_raw, pf_path, "command", nonempty_items=True, required=True, nonempty_list=True,
            )
            self.expect(preflight_raw, pf_path, "outputContains", str, optional=True)
            pf_env = self.expect_env_dict(preflight_raw, pf_path, "env")
            pf_timeout = self.expect_positive_int(preflight_raw, pf_path, "timeoutSeconds", default=30)
            preflight = dict(preflight_raw, command=command, env=pf_env, timeoutSeconds=pf_timeout)
        self.expect(cli_cfg, path, "outerModel", str, optional=optional, extra_origins=mode_origins)
        self.expect(cli_cfg, path, "outerLowQuotaFallback", str, optional=True)
        return dict(
            cli_cfg, fallbackBinaries=fallback_binaries, args=args, nonGitArgs=non_git_args,
            extraDirArgs=extra_dir_args, promptPrefixArgs=prompt_prefix_args, env=env,
            timeoutSeconds=timeout_seconds, preflight=preflight,
        )

    def resolve_mcp(self, role_id, mcp_cfg, mode, native):
        path = ("roles", role_id, "mcp")
        normalized = self._normalize_mcp_fields(mcp_cfg, path, mode)
        flag_value = self.expect(mcp_cfg, path, "fallbackToNativeOnTransientFailure", bool, optional=True)
        if flag_value and native is None:
            field = (*path, "fallbackToNativeOnTransientFailure")
            self.invalid(
                field,
                "requires a native configuration",
                self.latest_source(field, ("roles", role_id, "native")),
            )
        normalized["fallbackToNativeOnTransientFailure"] = bool(flag_value)
        for variant in CLI_VARIANTS:
            override = self.expect(mcp_cfg, path, variant, dict, optional=True)
            normalized[variant] = self._normalize_mcp_profile(normalized, override, (*path, variant), mode)
        return normalized

    def _normalize_mcp_profile(self, base, override, path, mode):
        if override is None:
            return None
        profile = {key: base[key] for key in MCP_VARIANT_FIELDS if key in base}
        profile.update(override)
        if profile.get("server") is None:
            profile["server"] = base["server"]
        return self._normalize_mcp_fields(profile, path, mode)

    def _normalize_mcp_fields(self, mcp_cfg, path, mode):
        optional = mode != "mcp"
        mode_origins = ((*path[:2], "mode"),) if not optional else ()
        self.expect(mcp_cfg, path, "provider", str, optional=optional, extra_origins=mode_origins)
        server = self.expect(mcp_cfg, path, "server", str, optional=True)
        if server is None:
            server = MCP_DEFAULT_SERVER
        self.expect(mcp_cfg, path, "model", str, optional=True)
        thinking = self.expect(mcp_cfg, path, "thinking", str, optional=True)
        if thinking is not None and thinking not in MCP_THINKING_LEVELS:
            levels = ", ".join(repr(level) for level in MCP_THINKING_LEVELS)
            self.invalid((*path, "thinking"), f"expected one of {levels}")
        wait_ms = self.expect_positive_int(mcp_cfg, path, "waitMs", default=None)
        self.expect(mcp_cfg, path, "outerModel", str, optional=True)
        self.expect(mcp_cfg, path, "outerLowQuotaFallback", str, optional=True)
        return dict(mcp_cfg, server=server, thinking=thinking, waitMs=wait_ms)

    def resolve_review(self, review_id, review):
        path = ("reviews", review_id)
        if not isinstance(review, dict):
            self.invalid(path, "expected a review object or null")
        self.expect(review, path, "description", str, optional=True)
        for key in ("onMissingProvider", "onIncomplete"):
            value = self.expect(review, path, key, str, optional=True)
            if value is not None and value not in ("block", "mark-incomplete"):
                self.invalid((*path, key), "expected 'block' or 'mark-incomplete'")
        rounds = self.expect(review, path, "maxRounds", int, optional=True)
        if rounds is not None and rounds < 1:
            self.invalid((*path, "maxRounds"), "must be at least 1")
        members = self.expect(review, path, "members", list)
        minimum = self.expect(review, path, "minDistinctProviders", int)
        distinct = self.expect(review, path, "requireDistinctProviders", bool, optional=True)
        allow_fallback = self.expect(review, path, "allowSameProviderFallback", bool, optional=True)
        if not members:
            self.invalid((*path, "members"), "review must have at least one member")
        member_roles = {}
        for index, member in enumerate(members):
            field = (*path, "members", index)
            if not isinstance(member, str) or member == "coordinator" or member not in self.roles:
                self.invalid(field, f"invalid or missing review member: {member!r}")
            if member in member_roles:
                self.invalid(field, f"duplicate review member: {member}")
            role = self.roles[member]
            if not role["readonly"]:
                self.invalid(
                    field,
                    f"review member {member!r} must be readonly",
                    self.latest_source(field, ("roles", member, "readonly")),
                )
            member_roles[member] = role
        if not 1 <= minimum <= len(members):
            self.invalid((*path, "minDistinctProviders"), "must be between 1 and the number of members")
        providers = {member: role["effectiveProvider"] for member, role in member_roles.items()}
        unique_providers = list(dict.fromkeys(providers.values()))
        provider_fields = [
            origin_path
            for name, role in member_roles.items()
            for origin_path in (("roles", name, "mode"), ("roles", name, role["mode"], "provider"))
        ]
        review_fields = tuple(
            field for key in ("members", "minDistinctProviders", "requireDistinctProviders")
            if (field := (*path, key)) in self.origins
        )
        fallback_field = (*path, "allowSameProviderFallback")
        fallback_fields = (fallback_field,) if fallback_field in self.origins else ()
        if distinct and len(unique_providers) < minimum:
            field = (*path, "minDistinctProviders")
            source = self.latest_source(*review_fields, *provider_fields)
            self.invalid(field, f"only {len(unique_providers)} distinct providers are available", source)
        if distinct and allow_fallback is not True:
            for member, role in member_roles.items():
                if role["mode"] != "native" or role["native"].get("lowQuotaFallback") is None:
                    continue
                field = ("roles", member, "native", "lowQuotaFallbackProvider")
                fallback = role["native"].get("lowQuotaFallbackProvider")
                if fallback is None:
                    self.invalid(
                        field,
                        f"required for Native fallback in review {review_id!r}",
                        self.latest_source(
                            field, ("roles", member, "mode"), ("roles", member, "native", "lowQuotaFallback"),
                        ),
                    )
                other_providers = {provider for name, provider in providers.items() if name != member}
                if fallback in other_providers or len(other_providers | {fallback}) < minimum:
                    source = self.latest_source(
                        field, ("roles", member, "mode"), ("roles", member, "native", "lowQuotaFallback"),
                        *review_fields, *fallback_fields, *provider_fields,
                    )
                    self.invalid(
                        field,
                        f"fallback would reuse a provider or leave too few distinct providers in review {review_id!r}",
                        source,
                    )
        if distinct:
            member_options = {member: {provider} for member, provider in providers.items()}
            for member, role in member_roles.items():
                mode_key = role["mode"]
                if mode_key in ("cli", "mcp"):
                    member_options[member].update(
                        role[mode_key][variant]["provider"]
                        for variant in CLI_VARIANTS
                        if role[mode_key][variant] is not None
                    )

            for member, role in member_roles.items():
                mode_key = role["mode"]
                if mode_key not in ("cli", "mcp"):
                    continue
                for variant in CLI_VARIANTS:
                    profile = role[mode_key][variant]
                    if profile is None:
                        continue
                    provider = profile["provider"]
                    if provider == providers[member]:
                        continue

                    colliding_others = [
                        name for name, options in member_options.items()
                        if name != member and provider in options
                    ]
                    reuses = bool(colliding_others) and not (variant == "fallback" and allow_fallback is True)
                    forced_other = {
                        providers[name] for name, options in member_options.items()
                        if name != member and provider not in options
                    }
                    if reuses or len(forced_other | {provider}) < minimum:
                        field = ("roles", member, mode_key, variant, "provider")
                        conflict_fields = [field, ("roles", member, "mode"), *review_fields]
                        if variant == "fallback":
                            conflict_fields.extend(fallback_fields)
                        for name in colliding_others or [name for name in member_roles if name != member]:
                            other_role = member_roles[name]
                            other_mode = other_role["mode"]
                            conflict_fields.append(("roles", name, "mode"))
                            if providers[name] == provider:
                                conflict_fields.append(("roles", name, other_mode, "provider"))
                            else:
                                matched_variant = (
                                    next(
                                        (
                                            other_variant
                                            for other_variant in CLI_VARIANTS
                                            if other_role[other_mode][other_variant] is not None
                                            and other_role[other_mode][other_variant]["provider"] == provider
                                        ),
                                        None,
                                    )
                                    if other_mode in ("cli", "mcp")
                                    else None
                                )
                                if matched_variant is not None:
                                    conflict_fields.append(("roles", name, other_mode, matched_variant, "provider"))
                                else:
                                    conflict_fields.append(("roles", name, other_mode, "provider"))
                        self.invalid(
                            field,
                            f"roles.{member}.{mode_key}.{variant}.provider {provider!r} "
                            f"violates review policy {review_id!r} distinct provider requirements",
                            self.latest_source(*conflict_fields),
                        )
        cli_roles = list(dict.fromkeys(
            member for member, role in member_roles.items() if role["mode"] == "cli"
        ))
        return dict(
            review,
            id=review_id,
            distinctProviders=unique_providers,
            requiresCliPreflight=any(role["requiresCliPreflight"] for role in member_roles.values()),
            cliRoles=cli_roles,
            memberRoles=list(member_roles.values()),
        )

    def validate_slots(self):
        for slot in (*ROLE_SLOTS, *REVIEW_SLOTS):
            path = ("slots", slot)
            if slot not in self.slots:
                self.invalid(path, "missing slot")
            target = self.expect(self.slots, ("slots",), slot, str, optional=slot != "coordinator")
            if target is None:
                continue
            if target == "coordinator" and slot != "coordinator":
                self.invalid(path, "reserved coordinator role")
            registry = "roles" if slot in ROLE_SLOTS else "reviews"
            entries = self.roles if registry == "roles" else self.reviews
            if target not in entries:
                source = self.latest_source(path, (registry, target))
                self.invalid(path, f"target {target!r} is missing or disabled", source)
            if slot == "coordinator" and self.roles[target]["workspace"] != "main":
                workspace_field = ("roles", target, "workspace")
                source = self.latest_source(path, workspace_field)
                if target == "coordinator":
                    path = workspace_field
                self.invalid(path, "coordinator slot requires workspace 'main'", source)
            if slot in ("recon", "plan") and not self.roles[target]["readonly"]:
                self.invalid(
                    path,
                    "slot requires a readonly role",
                    self.latest_source(path, ("roles", target, "readonly")),
                )

    @staticmethod
    def cli_role_summary(role_id, role):
        cli_cfg = role["cli"]
        return {
            "id": role_id,
            "provider": cli_cfg["provider"],
            "binary": cli_cfg["binary"],
            "readonly": role["readonly"],
            "hasPreflight": cli_cfg.get("preflight") is not None,
            "hasRework": cli_cfg.get("rework") is not None,
            "hasFallback": cli_cfg.get("fallback") is not None,
        }

    def cli_role_summaries(self):
        return [
            self.cli_role_summary(role_id, role)
            for role_id, role in self.roles.items()
            if role["mode"] == "cli"
        ]

    def selected_cli_roles(self, role_ids):
        if not role_ids:
            return self.cli_role_summaries(), None
        entries = []
        for role_id in dict.fromkeys(role_ids):
            if role_id not in self.roles:
                return None, role_id
            role = self.roles[role_id]
            cli_cfg = role.get("cli")
            if cli_cfg is None or not cli_cfg.get("provider") or not cli_cfg.get("binary"):
                self.invalid(("roles", role_id, "cli"), f"role {role_id!r} does not have a valid cli configuration")
            entries.append(self.cli_role_summary(role_id, role))
        return entries, None

    def dump(self):
        return dict(
            schemaVersion=2,
            pluginRoot=str(PLUGIN_ROOT),
            sources=self.sources,
            roles=self.roles,
            reviews=self.reviews,
            slots=self.slots,
            cliRoles=self.cli_role_summaries(),
        )


def parse_args():
    cli = argparse.ArgumentParser(prog="resolve-roles.sh", allow_abbrev=False)
    cli.add_argument("--cwd", default=os.getcwd(), metavar="DIR")
    cli.add_argument("--user-config-dir", metavar="DIR")
    cli.add_argument("--no-user-config", action="store_true")
    cli.add_argument("--project-config-dir", metavar="DIR")
    cli.add_argument("--no-project-config", action="store_true")
    cli.add_argument("--default-config", metavar="FILE")
    cli.add_argument("--allow-project-cli", action="store_true")
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("dump")
    for command, name in [("role", "ROLE_ID"), ("slot", "SLOT_NAME"), ("review", "REVIEW_ID")]:
        commands.add_parser(command).add_argument("name", metavar=name)
    cli_roles = commands.add_parser("cli-roles", allow_abbrev=False)
    cli_roles.add_argument("--format", choices=("json", "lines"), default="json")
    cli_roles.add_argument("roles", nargs="*", metavar="ROLE_ID")
    return cli.parse_args()


def cli_roles_output(entries, output_format):
    if output_format == "lines":
        return "".join(entry["id"] + "\n" for entry in entries)
    return json_output(entries)


def json_output(value):
    return json.dumps(value, separators=(",", ":")) + "\n"


def main():
    args = parse_args()
    try:
        resolver = Resolver(args)
        command = args.command
        if command == "cli-roles":
            entries, missing = resolver.selected_cli_roles(args.roles)
            if missing is not None:
                print(f"role {missing!r} is missing or disabled", file=sys.stderr)
                return 3
            output = cli_roles_output(entries, args.format)
        elif command == "dump":
            output = json_output(resolver.dump())
        elif command == "validate":
            output = json_output(dict(
                ok=True,
                sources=resolver.sources,
                roleCount=len(resolver.roles),
                reviewCount=len(resolver.reviews),
                cliRoleCount=sum(1 for role in resolver.roles.values() if role["mode"] == "cli"),
            ))
        else:
            name = args.name
            if command == "slot":
                target = resolver.slots.get(name)
                kind = "role" if name in ROLE_SLOTS else "review"
                registry = resolver.roles if kind == "role" else resolver.reviews
                result = None if target is None else {"slot": name, "kind": kind, "target": target, kind: registry[target]}
            else:
                registry = resolver.roles if command == "role" else resolver.reviews
                result = registry.get(name)
            if result is None:
                print(f"{command} {name!r} is missing or disabled", file=sys.stderr)
                return 3
            output = json_output(result)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
