# GBStack

GBStack is a portable agy plugin. It combines the on-demand Swarm Coordinator
rules, the `subagent-first` coordination workflow, and four agent prompts for
reconnaissance, planning, implementation, and review. Swarm coordination starts
when you invoke `/swarm` or resume an active Swarm.

## Install and migrate

If either predecessor is installed, disable it with agy before installing
GBStack to avoid duplicate rules, skills, and agents. You can also uninstall it
with `agy plugin uninstall <name>`.

```sh
agy plugin disable swarm-addon
agy plugin disable subagent-roles
```

Install the plugin from its GitHub repository (preferred).

```sh
agy plugin install https://github.com/lfld287/gbstack
```

You can also install the plugin from its local directory.

```sh
agy plugin install /path/to/gbstack
```

## Role configuration

GBStack merges three layers in order of increasing priority.

1. Plugin defaults in [`config/roles.default.json`](config/roles.default.json)
2. User overrides in `${GBSTACK_USER_CONFIG_DIR:-$HOME/.gemini/config/gbstack}/roles.json`
3. Project overrides in `<repo>/.agents/gbstack/roles.json`

Relative prompt names resolve under `agents/` and profile templates under
`templates/codex/` in those same layer directories. Lookup starts with the
project, then checks the user directory and the plugin root.

For example, add the following to the project `roles.json` to route
implementation to a custom `frontend` role and disable the monitor slot.
Replace the provider and model values with ones available in your host.

```json
{
  "roles": {
    "frontend": {
      "prompt": "worker.md",
      "readonly": false,
      "workspace": "inherit",
      "concurrentWorkspace": "share",
      "mode": "native",
      "native": {
        "provider": "your-provider",
        "model": "your-native-model"
      }
    }
  },
  "slots": {
    "implement": "frontend",
    "monitor": null
  }
}
```

From the plugin root, use `--cwd` to select the project configuration.

```sh
scripts/resolve-roles.sh --cwd /path/to/repo validate
scripts/resolve-roles.sh --cwd /path/to/repo dump
scripts/resolve-roles.sh --cwd /path/to/repo role frontend
scripts/resolve-roles.sh --cwd /path/to/repo slot implement
scripts/resolve-roles.sh --cwd /path/to/repo codex-profiles
```

In single-agent mode, the host's `invoke_subagent` discovery is limited to the
plugin's static `agents/*.md` files. Custom roles in `roles.json` are available
to Swarm and other dispatch that uses `resolve-roles.sh`.

## Codex profiles

Role resolution requires Bash and Python 3.8+. The Coordinator uses
`resolve-roles.sh` to select profiles and runs Codex preflight when the
resolved role or review has `requiresCodexPreflight` enabled. The bundled
configuration uses Codex for implementation, council, and explicit Codex
review, and Native members for routine review.

Use `resolve-roles.sh codex-profiles` to list active Codex profiles and their
resolved template paths. By default, `setup-codex.sh` and `check-codex.sh` use
`resolve-roles.sh` to read the merged role configuration and select the default
and extra profiles from roles in Codex mode. If no profiles are active, both
scripts print a skip message and exit 0.

```sh
/path/to/gbstack/scripts/setup-codex.sh --cwd /path/to/repo --dry-run
/path/to/gbstack/scripts/setup-codex.sh
/path/to/gbstack/scripts/check-codex.sh
/path/to/gbstack/scripts/check-codex.sh --codex-home "/path/to/codex-home" reviewer-codex
```

- Both scripts accept positional `[PROFILE ...]` arguments to select specific
  profiles. Setup resolves templates for those names through the same layers.
  Check uses the supplied names directly and bypasses role resolution.
- Setup skips automatically selected profiles that have no template. If any
  explicitly selected profile has no template, setup fails before creating the
  Codex home or installing a profile. It skips existing profiles by default,
  `--force` replaces them, and `--dry-run` previews actions without writes.
- Check looks for an executable Codex on `PATH` and then at
  `$HOME/.local/bin/codex`. It requires a successful `codex login status`
  containing `Logged in` and a readable file for each selected profile.
  During the login query it sets `TMPDIR` to the resolved Codex home so release
  builds skip creating temporary PATH aliases. Codex may print a warning
  about the skipped aliases even when the check succeeds.
- Both scripts pass `--cwd DIR`, `--user-config-dir DIR`, `--project-config-dir DIR`,
  `--no-user-config`, and `--no-project-config` to the resolver when they call it.
  Both accept `--codex-home DIR` to override `${CODEX_HOME:-$HOME/.codex}` and
  `-h` / `--help` to print usage. Check uses the Codex home for both login status
  and profile lookup.

## Playbooks and engineering skills

The built-in router in [subagent-first](skills/subagent-first/SKILL.md) matches
Swarm work to one of eight [on-demand playbooks](skills/subagent-first/playbooks/).
Read the matching playbook before breaking down the work or writing briefs. If
none applies, follow the standard `subagent-first` workflow.

Eleven standalone engineering skills cover code tracing (`how`), design history
(`why`), system explanation (`teach`), architecture (`architect`), change
impact (`blast-radius`), design critique (`interrogate`), editing (`unslop`),
context recovery (`ketchup`), course correction (`correct`), requirements
refinement (`sharpen`), and multi-provider iterative review (`council`). Use
them on their own or reference them in Swarm briefs.

## Directory structure

```text
gbstack/
├── .gitignore
├── plugin.json
├── README.md
├── agents/
│   ├── scout.md
│   ├── plan.md
│   ├── worker.md
│   └── reviewer.md
├── config/
│   └── roles.default.json
├── rules/
│   └── swarm-addon.md
├── scripts/
│   ├── check-codex.sh
│   ├── lib/
│   │   └── resolve_roles.py
│   ├── resolve-roles.sh
│   └── setup-codex.sh
├── skills/
│   ├── architect/
│   │   └── SKILL.md
│   ├── blast-radius/
│   │   └── SKILL.md
│   ├── correct/
│   │   └── SKILL.md
│   ├── council/
│   │   └── SKILL.md
│   ├── how/
│   │   └── SKILL.md
│   ├── interrogate/
│   │   └── SKILL.md
│   ├── ketchup/
│   │   └── SKILL.md
│   ├── sharpen/
│   │   └── SKILL.md
│   ├── subagent-first/
│   │   ├── SKILL.md
│   │   └── playbooks/
│   │       ├── authoring-a-skill.md
│   │       ├── bug-fix.md
│   │       ├── feature.md
│   │       ├── investigation.md
│   │       ├── perf-issue.md
│   │       ├── pr-review.md
│   │       ├── prototype.md
│   │       └── refactoring.md
│   ├── teach/
│   │   └── SKILL.md
│   ├── unslop/
│   │   └── SKILL.md
│   └── why/
│       └── SKILL.md
├── templates/
│   └── codex/
│       ├── worker-codex.config.toml
│       ├── worker-codex-xhigh.config.toml
│       └── reviewer-codex.config.toml
└── tests/
    ├── test_codex_scripts.py
    └── test_resolve_roles.py
```

## Optional external enhancements

For explicit Orca requests, GBStack uses `orchestration` for dispatch and
`orca-cli` for worktrees, terminals, or one-way handoffs when the relevant
skill is installed. Otherwise it follows the usual workflow for the request.
It uses `long-answer-html` to format final reports when installed and replies
normally when that skill is unavailable.
