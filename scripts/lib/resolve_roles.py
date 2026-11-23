"""Resolve GBStack roles, review groups, and Codex profiles from local config."""

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
REVIEW_SLOTS = ("review", "interrogate", "codexReview", "council")
ROLE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
NATIVE_FALLBACK_FLAGS = ("fallbackToNativeOnPreflightFail", "fallbackToNativeOnTransientFailure")
FIELDS = {
    (): ("schemaVersion", "roles", "reviews", "slots"),
    ("slots",): ROLE_SLOTS + REVIEW_SLOTS,
    ("roles", "*"): (
        "description", "prompt", "skill", "readonly", "workspace", "concurrentWorkspace",
        "mode", "native", "codex",
    ),
    ("roles", "*", "native"): ("provider", "model", "reworkModel", "lowQuotaFallback", "lowQuotaFallbackProvider"),
    ("roles", "*", "codex"): (
        "provider", "profile", "extraProfiles", "sandbox", "outerModel", "outerLowQuotaFallback",
    ) + NATIVE_FALLBACK_FLAGS,
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
        if type(version) is not int or version != 1:
            raise ConfigError(source, ("schemaVersion",), "expected schema version 1")
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
            if any(os.path.lexists(candidate / item) for item in ("roles.json", "agents", "templates/codex")):
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


class Resolver:
    def __init__(self, args):
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
        self.profiles = {}
        self.roles = {
            role_id: self.resolve_role(role_id, role)
            for role_id, role in role_registry.items() if role is not None
        }
        self.reviews = {
            review_id: self.resolve_review(review_id, review)
            for review_id, review in review_registry.items() if review is not None
        }
        self.validate_slots()
        self.profile_entries = {name: self.resolve_profile(name) for name in self.profiles}

    def merge(self, base, update, source, path):
        shape = path
        if len(path) > 1 and path[0] in ("roles", "reviews"):
            shape = (path[0], "*", *path[2:])
        allowed = FIELDS.get(shape)
        for key, new in update.items():
            field = (*path, key)
            if allowed is not None and key not in allowed:
                raise ConfigError(source, field, "unknown key")
            if path == ("roles",) and (not ROLE_ID.fullmatch(key) or key == "all" or key.startswith("coordinator-")):
                raise ConfigError(source, field, "invalid or reserved role ID")
            old = base.get(key)
            if old is not None and new is not None and type(old) is not type(new):
                raise ConfigError(source, field, f"type conflict ({type(old).__name__} vs. {type(new).__name__})")
            self.origins[field] = source
            if isinstance(new, dict):
                base[key] = {} if old is None else old
                self.merge(base[key], new, source, field)
            else:
                if isinstance(old, dict):
                    for descendant in list(self.origins):
                        if len(descendant) > len(field) and descendant[:len(field)] == field:
                            del self.origins[descendant]
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

    def expect(self, node, path, key, kind, optional=False):
        value = node.get(key)
        if value is None and optional:
            return None
        if type(value) is not kind or (kind is str and not value):
            self.invalid((*path, key), f"expected {'nonempty ' if kind is str else ''}{kind.__name__}")
        return value

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
        readonly = self.expect(role, path, "readonly", bool)
        self.expect(role, path, "workspace", str)
        mode = self.expect(role, path, "mode", str)
        if mode not in ("native", "codex"):
            self.invalid((*path, "mode"), "expected 'native' or 'codex'")
        native = self.expect(role, path, "native", dict, optional=mode != "native")
        codex = self.expect(role, path, "codex", dict, optional=mode != "codex")
        resolved = dict(role)
        if native is not None:
            native_path = (*path, "native")
            self.expect(native, native_path, "provider", str)
            model = self.expect(native, native_path, "model", str)
            for key in ("reworkModel", "lowQuotaFallback", "lowQuotaFallbackProvider"):
                self.expect(native, native_path, key, str, optional=True)
            resolved["native"] = dict(native)
            if resolved["native"].get("reworkModel") is None:
                resolved["native"]["reworkModel"] = model
        if codex is not None:
            self.register_profiles(role_id, codex, readonly, mode, native)
        prompt = role.get("prompt", f"{role_id}.md")
        prompt_path, prompt_source = self.resolve_prompt(prompt, (*path, "prompt"))
        return dict(
            resolved, id=role_id, prompt=prompt, promptPath=prompt_path, promptSource=prompt_source,
            effectiveProvider=role[mode]["provider"], requiresCodexPreflight=mode == "codex",
        )

    def register_profiles(self, role_id, codex, readonly, mode, native):
        path = ("roles", role_id, "codex")
        profile = self.expect(codex, path, "profile", str, optional=mode != "codex")
        extra = self.expect(codex, path, "extraProfiles", list, optional=True) or []
        self.expect(codex, path, "provider", str, optional=mode != "codex")
        sandbox = self.expect(codex, path, "sandbox", str, optional=profile is None and not extra)
        if sandbox is not None and sandbox not in ("read-only", "workspace-write"):
            self.invalid((*path, "sandbox"), "expected 'read-only' or 'workspace-write'")
        for key in ("outerModel", "outerLowQuotaFallback"):
            self.expect(codex, path, key, str, optional=True)
        for flag in NATIVE_FALLBACK_FLAGS:
            if self.expect(codex, path, flag, bool, optional=True) and native is None:
                field = (*path, flag)
                self.invalid(field, "requires a native configuration",
                             self.latest_source(field, ("roles", role_id, "native")))
        if (readonly and sandbox != "read-only"
                and (mode == "codex" or profile is not None or extra or sandbox is not None)):
            field = (*path, "sandbox")
            self.invalid(field, "readonly roles require the read-only sandbox",
                         self.latest_source(field, ("roles", role_id, "readonly")))
        profiles = [(name, (*path, "extraProfiles", index)) for index, name in enumerate(extra)]
        if profile is not None:
            profiles.insert(0, (profile, (*path, "profile")))
        seen = set()
        for name, field in profiles:
            if not isinstance(name, str) or not PROFILE_NAME.fullmatch(name):
                self.invalid(field, f"invalid Codex profile name: {name!r}")
            if name in seen:
                self.invalid(field, f"duplicate Codex profile: {name}")
            seen.add(name)
            if mode != "codex":
                continue
            entry = self.profiles.setdefault(name, {"sandbox": sandbox, "roles": [], "field": field})
            if entry["sandbox"] != sandbox:
                other = entry["roles"][0]
                source = self.latest_source(field, (*path, "sandbox"), entry["field"],
                                            ("roles", other, "codex", "sandbox"))
                self.invalid(field, f"Codex profile {name!r} uses a different sandbox in role {other!r}", source)
            entry["roles"].append(role_id)

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
                self.invalid(field, f"review member {member!r} must be readonly",
                             self.latest_source(field, ("roles", member, "readonly")))
            member_roles[member] = role
        if not 1 <= minimum <= len(members):
            self.invalid((*path, "minDistinctProviders"), "must be between 1 and the number of members")
        providers = {member: role["effectiveProvider"] for member, role in member_roles.items()}
        unique_providers = list(dict.fromkeys(providers.values()))
        provider_fields = [("roles", name, role["mode"], "provider") for name, role in member_roles.items()]
        if distinct and len(unique_providers) < minimum:
            field = (*path, "minDistinctProviders")
            source = self.latest_source(field, (*path, "members"), (*path, "requireDistinctProviders"),
                                        *provider_fields)
            self.invalid(field, f"only {len(unique_providers)} distinct providers are available", source)
        if distinct and allow_fallback is not True:
            for member, role in member_roles.items():
                if role["mode"] != "native" or role["native"].get("lowQuotaFallback") is None:
                    continue
                field = ("roles", member, "native", "lowQuotaFallbackProvider")
                fallback = role["native"].get("lowQuotaFallbackProvider")
                if fallback is None:
                    self.invalid(field, f"required for Native fallback in review {review_id!r}",
                                 self.latest_source(field, ("roles", member, "native", "lowQuotaFallback")))
                other_providers = {provider for name, provider in providers.items() if name != member}
                if fallback in other_providers or len(other_providers | {fallback}) < minimum:
                    source = self.latest_source(
                        field, ("roles", member, "native", "lowQuotaFallback"), (*path, "members"),
                        (*path, "minDistinctProviders"), (*path, "requireDistinctProviders"),
                        (*path, "allowSameProviderFallback"), *provider_fields,
                    )
                    self.invalid(field, f"fallback would reuse a provider or leave too few distinct providers "
                                        f"in review {review_id!r}", source)
        profiles = {}
        for role in member_roles.values():
            if role["requiresCodexPreflight"]:
                profiles[role["codex"]["profile"]] = None
        return dict(
            review, id=review_id, distinctProviders=unique_providers, requiresCodexPreflight=bool(profiles),
            codexProfiles=list(profiles), memberRoles=list(member_roles.values()),
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
                self.invalid(path, "slot requires a readonly role",
                             self.latest_source(path, ("roles", target, "readonly")))

    def resolve_profile(self, name):
        path, layer = self.lookup_asset(Path("templates/codex") / f"{name}.config.toml",
                                        ("codexProfiles", name, "templatePath"))
        metadata = self.profiles.get(name, {"sandbox": None, "roles": []})
        return dict(profile=name, sandbox=metadata["sandbox"], roles=metadata["roles"],
                    templatePath=path, templateSource=layer)

    def selected_profiles(self, names):
        if not names:
            return list(self.profile_entries.values())
        return [self.profile_entries.get(name) or self.resolve_profile(name) for name in dict.fromkeys(names)]

    def dump(self):
        return dict(schemaVersion=1, pluginRoot=str(PLUGIN_ROOT), sources=self.sources, roles=self.roles,
                    reviews=self.reviews, slots=self.slots, codexProfiles=list(self.profile_entries.values()))


def profile_argument(value):
    if not PROFILE_NAME.fullmatch(value):
        raise argparse.ArgumentTypeError(f"invalid Codex profile name: {value!r}")
    return value


def parse_args():
    cli = argparse.ArgumentParser(prog="resolve-roles.sh", allow_abbrev=False)
    cli.add_argument("--cwd", default=os.getcwd(), metavar="DIR")
    cli.add_argument("--user-config-dir", metavar="DIR")
    cli.add_argument("--no-user-config", action="store_true")
    cli.add_argument("--project-config-dir", metavar="DIR")
    cli.add_argument("--no-project-config", action="store_true")
    cli.add_argument("--default-config", metavar="FILE")
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("dump")
    for command, name in [("role", "ROLE_ID"), ("slot", "SLOT_NAME"), ("review", "REVIEW_ID")]:
        commands.add_parser(command).add_argument("name", metavar=name)
    profiles = commands.add_parser("codex-profiles", allow_abbrev=False)
    profiles.add_argument("--format", choices=("json", "lines", "tsv"), default="json")
    profiles.add_argument("profiles", nargs="*", metavar="PROFILE", type=profile_argument)
    return cli.parse_args()


def profile_output(entries, output_format):
    if output_format == "lines":
        return "".join(entry["profile"] + "\n" for entry in entries)
    if output_format == "tsv":
        rows = []
        for entry in entries:
            path = entry["templatePath"] or ""
            source = entry["templateSource"] or ""
            if any(char in path for char in "\t\n\r"):
                raise ConfigError((source, path), ("codexProfiles", entry["profile"], "templatePath"),
                                  "template path contains a tab or line break")
            rows.append("\t".join((entry["profile"], path, source)) + "\n")
        return "".join(rows)
    return json_output(entries)


def json_output(value):
    return json.dumps(value, separators=(",", ":")) + "\n"


def main():
    args = parse_args()
    try:
        resolver = Resolver(args)
        command = args.command
        if command == "codex-profiles":
            output = profile_output(resolver.selected_profiles(args.profiles), args.format)
        elif command == "dump":
            output = json_output(resolver.dump())
        elif command == "validate":
            output = json_output(dict(ok=True, sources=resolver.sources, roleCount=len(resolver.roles),
                                      reviewCount=len(resolver.reviews), codexProfileCount=len(resolver.profiles)))
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
