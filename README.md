# GBStack

GBStack is a portable agy plugin. It combines on-demand Swarm Coordinator
rules, the `subagent-first` coordination workflow, four specialized agent
prompts (`scout`, `plan`, `worker`, `reviewer`), 8 on-demand playbooks, 11
standalone engineering skills, and Schema v2 dynamic CLI role execution. Swarm
coordination starts when you invoke `/swarm` or resume an active Swarm.

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

## Architecture (Schema v2)

GBStack Schema v2 (`"schemaVersion": 2`, v1 is no longer supported) removes all
CLI-specific hardcoding. Every role runs in either `"native"` or `"cli"` mode:

- **`mode: "native"`**: Runs directly on a native host model (`native.model`).
- **`mode: "cli"`**: Uses a thin Swarm outer driver (default
  `outerModel: "gemini-3.8-flash-medium"`, reusing Swarm native
  `concurrentWorkspace: "share"` for isolated concurrent writes) that invokes
  the unified deterministic runner `scripts/run-cli.sh`. Session-level readiness
  checks run through `scripts/preflight-cli.sh`.

### Role configuration and 3-layer merge

GBStack merges three layers in order of increasing priority:

1. Plugin defaults in [`config/roles.default.json`](config/roles.default.json)
2. User overrides in `${GBSTACK_USER_CONFIG_DIR:-$HOME/.gemini/config/gbstack}/roles.json`
3. Project overrides in `<repo>/.agents/gbstack/roles.json`

Relative `prompt` names resolve under `agents/` in those same layer
directories (project -> user -> plugin root).

**Project-layer `cli` security boundary:** By default, project-level
`<repo>/.agents/gbstack/roles.json` is forbidden from defining or overriding any
non-`null` `roles.*.cli` block (preventing untrusted repositories from
injecting arbitrary shell commands); only the plugin default and user layers are
trusted for `cli` definitions. Project overrides may still change `mode`
(switching between default/user-defined `native` and `cli`), `native`, `prompt`,
`reviews`, `slots`, or set `"cli": null`. To explicitly allow project-level
`roles.*.cli`, pass `--allow-project-cli` to `resolve-roles.sh` /
`preflight-cli.sh` / `run-cli.sh` or set `GBSTACK_ALLOW_PROJECT_CLI=1`.

**Built-in slots:**
- Role slots (`ROLE_SLOTS`): `coordinator`, `recon`, `plan`, `implement`, `monitor`
- Review slots (`REVIEW_SLOTS`): `review`, `interrogate`, `council`

When a user explicitly requests review by a specific CLI tool (such as `codex`,
`pi`, `omp`, or `claude`), resolve it directly via `resolve-roles.sh role <id>`
or a custom `review <id>` entry rather than a dedicated built-in slot.

The default `roles.default.json` configures `worker` (Codex inline args,
`outerModel: "gemini-3.8-flash-medium"`, with Native fallback) and
`reviewer-codex` (Codex inline read-only args,
`outerModel: "gemini-3.8-flash-medium"`, used in `council`) as built-in CLI role
examples.

### Resolver commands

Role resolution requires Bash and Python 3.8+. From the plugin root, use
`--cwd` to select the project configuration:

```sh
scripts/resolve-roles.sh --cwd /path/to/repo validate
scripts/resolve-roles.sh --cwd /path/to/repo dump
scripts/resolve-roles.sh --cwd /path/to/repo role worker
scripts/resolve-roles.sh --cwd /path/to/repo slot implement
scripts/resolve-roles.sh --cwd /path/to/repo review council
scripts/resolve-roles.sh --cwd /path/to/repo cli-roles --format json
scripts/resolve-roles.sh --cwd /path/to/repo cli-roles --format lines
```

In single-agent mode, the host's `invoke_subagent` discovery is limited to the
plugin's static `agents/*.md` files. Custom roles in `roles.json` are available
to Swarm and other dispatch that uses `resolve-roles.sh`.

## Dynamic CLI configuration (`roles.*.cli`)

A role with `"mode": "cli"` defines its behavior under `roles.<id>.cli`:

| Field | Description |
|---|---|
| `provider` | Underlying model provider identifier (e.g., `"openai"`, `"anthropic"`, `"google"`) — must name the model provider rather than the CLI tool name, as it is used for heterogeneous review provider counting |
| `binary` | Primary executable name or path |
| `fallbackBinaries` | Ordered fallback executable paths checked if `binary` is not on `PATH` |
| `args` | Base CLI arguments; supports `{workdir}` and `{binary}` placeholders |
| `nonGitArgs` | Extra arguments appended automatically when `--cwd` is outside a Git worktree |
| `extraDirArgs` | Argument template per `--add-dir`; supports `{dir}` placeholder |
| `promptInput` | `"stdin"` or `"arg"` |
| `stdinArg` | Optional flag/token appended when `promptInput` is `"stdin"` (e.g., `"-"`) |
| `promptPrefixArgs` | Arguments placed immediately before the prompt argument when `promptInput` is `"arg"` |
| `env` | Map of environment variables injected when executing the CLI |
| `timeoutSeconds` | Per-invocation wall-clock timeout in seconds |
| `preflight` | Object `{command, outputContains, env, timeoutSeconds}` for `preflight-cli.sh` |
| `outerModel` | **Required** when `mode: "cli"` (the resolver does not auto-fill a default; `config/roles.default.json` uses `"gemini-3.8-flash-medium"`) |
| `outerLowQuotaFallback` | Fallback outer driver model on quota exhaustion |
| `fallbackToNativeOnPreflightFail` | Whether Coordinator may fall back to `native` if preflight fails |
| `fallbackToNativeOnTransientFailure` | Whether Coordinator may fall back to `native` on zero-diff transient CLI failure |

## Preflight and runner scripts

### `scripts/preflight-cli.sh`

Run session-level read-only preflight checks when resolved roles or reviews have
`requiresCliPreflight: true` (inspect active CLI roles via `cliRoles`):

```sh
scripts/preflight-cli.sh --cwd /path/to/repo --role worker
scripts/preflight-cli.sh --cwd /path/to/repo --review council
scripts/preflight-cli.sh --cwd /path/to/repo --all
```

Exit codes: `0` passed, `1` preflight or config failure, `2` invalid arguments,
`3` missing or disabled role/review, `127` executable not found.

### `scripts/run-cli.sh`

Executed by the thin Swarm outer driver (`agents/worker.md` or
`agents/reviewer.md`) in `执行模式: CLI`, reading the self-contained prompt from
stdin:

```sh
scripts/run-cli.sh \
  --role '<ROLE_ID>' \
  --cwd '<WORKDIR>' \
  [--add-dir '<DIR>' ...] \
  [--timeout <SEC>] \
  [--baseline-json '<JSON>']
```

`run-cli.sh` resolves the binary, verifies the pre-write baseline (when
`--baseline-json` is provided), injects Git/non-Git and `--add-dir` flags,
enforces process-group timeouts, detects `readonly` workspace tampering and
empty output, trims oversized output, and emits a trailing machine-readable block
(note: Git snapshots use `git status -uall`, so `gitignored` files are not
tracked in `changedFiles`):

```text
--- GBSTACK_CLI_RESULT_JSON ---
{"role":"...","binary":"...","exitCode":0,"timedOut":false,"durationMs":1234,"emptyOutput":false,"readonlyViolation":false,"workdir":"...","changedFiles":[],"outOfScopeFiles":[]}
```

**`--baseline-json` contract:** Only the following four keys are accepted; any
unknown key is rejected immediately with exit code `2` before launching the
inner CLI:

- `gitCleanHead`: Full `HEAD` SHA string for clean Git workspaces (`git status --porcelain` must be empty and `HEAD` must match).
- `fileSha1`: Object mapping relative paths (under `--cwd`; `.` and symlinks are forbidden) to expected SHA-1 hex digests.
- `plannedNew`: Array of relative paths (`.` forbidden) that must not exist before execution (including dangling symlinks).
- `allowedPaths`: Array of allowed relative paths/directory prefixes (or `["."]` for the entire `--cwd` workspace).

Exit codes:
- `0`: Success
- `1`: CLI non-zero exit or configuration error
- `2`: Invalid arguments or pre-write baseline mismatch (including unknown `--baseline-json` keys)
- `3`: Role missing or disabled
- `4`: `readonly` role modified the workspace (`readonlyViolation`)
- `5`: `readonly` role produced empty output (`emptyOutput`; must mark `未审完:`)
- `124`: Timed out (process group terminated)
- `127`: Executable not found
- `128+N`: Terminated by an external signal (`SIGTERM`/`SIGINT`); process group is cleaned up and `run-cli.sh` exits immediately without necessarily printing `GBSTACK_CLI_RESULT_JSON`

## Copy-paste `roles.json` snippets

Add any of the following snippets to `${GBSTACK_USER_CONFIG_DIR:-$HOME/.gemini/config/gbstack}/roles.json`
or `<repo>/.agents/gbstack/roles.json` (`"schemaVersion": 2`; note that
project-level `roles.*.cli` definitions require `--allow-project-cli` or
`GBSTACK_ALLOW_PROJECT_CLI=1`).

> **Note on `cli.provider`:** Always set `cli.provider` to the underlying
> model provider (such as `"anthropic"` for Claude models or `"openai"` for GPT
> models), **not** the CLI tool name (`"pi"`, `"omp"`, etc.), because GBStack
> uses `cli.provider` to count distinct providers in heterogeneous reviews.

### Codex (`codex exec`)

```json
{
  "schemaVersion": 2,
  "roles": {
    "worker-codex": {
      "description": "Implementation worker via Codex CLI",
      "prompt": "worker.md",
      "readonly": false,
      "workspace": "inherit",
      "concurrentWorkspace": "share",
      "mode": "cli",
      "native": {
        "provider": "google",
        "model": "gemini-4-hydrogen-high",
        "reworkModel": "gemini-4-neon-high",
        "lowQuotaFallback": "claude-sonnet-5-5-high"
      },
      "cli": {
        "provider": "openai",
        "binary": "codex",
        "fallbackBinaries": ["~/.local/bin/codex"],
        "args": [
          "exec",
          "--ephemeral",
          "--color", "never",
          "-s", "workspace-write",
          "-c", "approval_policy=\"never\"",
          "-c", "model=\"gpt-6.2-sol\"",
          "-c", "model_reasoning_effort=\"high\"",
          "-C", "{workdir}"
        ],
        "nonGitArgs": ["--skip-git-repo-check"],
        "extraDirArgs": ["--add-dir", "{dir}"],
        "promptInput": "stdin",
        "stdinArg": "-",
        "promptPrefixArgs": [],
        "env": {},
        "timeoutSeconds": 600,
        "preflight": {
          "command": ["{binary}", "login", "status"],
          "outputContains": "Logged in",
          "env": {},
          "timeoutSeconds": 10
        },
        "outerModel": "gemini-3.8-flash-medium",
        "outerLowQuotaFallback": "claude-sonnet-5-5-high",
        "fallbackToNativeOnPreflightFail": true,
        "fallbackToNativeOnTransientFailure": true
      }
    }
  }
}
```

### Pi (`pi -p`)

```json
{
  "schemaVersion": 2,
  "roles": {
    "worker-pi": {
      "description": "Implementation worker via Pi CLI",
      "prompt": "worker.md",
      "readonly": false,
      "workspace": "inherit",
      "concurrentWorkspace": "share",
      "mode": "cli",
      "native": {
        "provider": "google",
        "model": "gemini-4-hydrogen-high",
        "reworkModel": "gemini-4-neon-high",
        "lowQuotaFallback": "claude-sonnet-5-5-high"
      },
      "cli": {
        "provider": "anthropic",
        "binary": "pi",
        "fallbackBinaries": [],
        "args": ["-p", "--no-session", "--model", "anthropic/claude-sonnet-4-5"],
        "nonGitArgs": [],
        "extraDirArgs": [],
        "promptInput": "arg",
        "stdinArg": null,
        "promptPrefixArgs": ["--"],
        "env": {},
        "timeoutSeconds": 600,
        "preflight": {
          "command": ["{binary}", "--version"],
          "env": {},
          "timeoutSeconds": 5
        },
        "outerModel": "gemini-3.8-flash-medium",
        "outerLowQuotaFallback": "claude-sonnet-5-5-high",
        "fallbackToNativeOnPreflightFail": true,
        "fallbackToNativeOnTransientFailure": true
      }
    },
    "reviewer-pi": {
      "description": "Read-only reviewer via Pi CLI",
      "prompt": "reviewer.md",
      "readonly": true,
      "workspace": "inherit",
      "mode": "cli",
      "cli": {
        "provider": "anthropic",
        "binary": "pi",
        "fallbackBinaries": [],
        "args": [
          "-p",
          "--no-session",
          "--tools", "read,grep,find,ls",
          "--model", "anthropic/claude-opus-4-5"
        ],
        "nonGitArgs": [],
        "extraDirArgs": [],
        "promptInput": "arg",
        "stdinArg": null,
        "promptPrefixArgs": ["--"],
        "env": {},
        "timeoutSeconds": 300,
        "preflight": {
          "command": ["{binary}", "--version"],
          "env": {},
          "timeoutSeconds": 5
        },
        "outerModel": "gemini-3.8-flash-medium",
        "outerLowQuotaFallback": "claude-sonnet-5-5-high",
        "fallbackToNativeOnPreflightFail": false,
        "fallbackToNativeOnTransientFailure": false
      }
    }
  }
}
```

### OMP (`omp -p`)

```json
{
  "schemaVersion": 2,
  "roles": {
    "worker-omp": {
      "description": "Implementation worker via OMP CLI",
      "prompt": "worker.md",
      "readonly": false,
      "workspace": "inherit",
      "concurrentWorkspace": "share",
      "mode": "cli",
      "native": {
        "provider": "google",
        "model": "gemini-4-hydrogen-high",
        "reworkModel": "gemini-4-neon-high",
        "lowQuotaFallback": "claude-sonnet-5-5-high"
      },
      "cli": {
        "provider": "openai",
        "binary": "omp",
        "fallbackBinaries": ["~/.local/bin/omp"],
        "args": ["-p", "--no-session", "--cwd", "{workdir}", "--model", "openai/gpt-5.2"],
        "nonGitArgs": [],
        "extraDirArgs": ["--add-dir", "{dir}"],
        "promptInput": "arg",
        "stdinArg": null,
        "promptPrefixArgs": [],
        "env": {},
        "timeoutSeconds": 600,
        "preflight": {
          "command": ["{binary}", "--version"],
          "env": {},
          "timeoutSeconds": 5
        },
        "outerModel": "gemini-3.8-flash-medium",
        "outerLowQuotaFallback": "claude-sonnet-5-5-high",
        "fallbackToNativeOnPreflightFail": true,
        "fallbackToNativeOnTransientFailure": true
      }
    }
  }
}
```

### Claude Code (`claude -p`)

```json
{
  "schemaVersion": 2,
  "roles": {
    "worker-claude": {
      "description": "Implementation worker via Claude Code CLI",
      "prompt": "worker.md",
      "readonly": false,
      "workspace": "inherit",
      "concurrentWorkspace": "share",
      "mode": "cli",
      "native": {
        "provider": "google",
        "model": "gemini-4-hydrogen-high",
        "reworkModel": "gemini-4-neon-high",
        "lowQuotaFallback": "claude-sonnet-5-5-high"
      },
      "cli": {
        "provider": "anthropic",
        "binary": "claude",
        "fallbackBinaries": ["~/.local/bin/claude"],
        "args": ["-p", "--output-format", "text"],
        "nonGitArgs": [],
        "extraDirArgs": ["--add-dir", "{dir}"],
        "promptInput": "stdin",
        "stdinArg": null,
        "promptPrefixArgs": [],
        "env": {},
        "timeoutSeconds": 600,
        "preflight": {
          "command": ["{binary}", "--version"],
          "env": {},
          "timeoutSeconds": 5
        },
        "outerModel": "gemini-3.8-flash-medium",
        "outerLowQuotaFallback": "claude-sonnet-5-5-high",
        "fallbackToNativeOnPreflightFail": true,
        "fallbackToNativeOnTransientFailure": true
      }
    }
  }
}
```

## Migrating from Schema v1 to Schema v2

Schema v1 configurations are no longer supported. Update your user and project
`roles.json` files as follows:

| Schema v1 (removed) | Schema v2 (current) |
|---|---|
| `"schemaVersion": 1` (or omitted) | `"schemaVersion": 2` (required in `config/roles.default.json`; optional in user/project overrides, must be `2` if set) |
| `"mode": "codex"` and `"codex": { "profile", "sandbox", ... }` | `"mode": "cli"` and `"cli": { "provider", "binary", "args", ... }` |
| `templates/codex/*.config.toml` + `setup-codex.sh` | Inline CLI arguments and flags in `roles.*.cli.args` (no template files) |
| `scripts/check-codex.sh` | `scripts/preflight-cli.sh` using `roles.*.cli.preflight` |
| `resolve-roles.sh codex-profiles` | `resolve-roles.sh cli-roles [--format json\|lines]` |
| `requiresCodexPreflight` / `codexProfiles` | `requiresCliPreflight` / `cliRoles` |
| `slots.codexReview` / `reviews.codex` | Resolve directly via `resolve-roles.sh role <id>` or custom `review <id>` |
| Brief marker `执行模式: Codex` + `PROFILE` / `SANDBOX` | Brief marker `执行模式: CLI` + `ROLE_ID` + `PLUGIN_ROOT` (+ `--baseline-json`) |

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
│   ├── lib/
│   │   ├── resolve_roles.py
│   │   └── run_cli.py
│   ├── preflight-cli.sh
│   ├── resolve-roles.sh
│   └── run-cli.sh
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
└── tests/
    ├── test_cli_runner.py
    └── test_resolve_roles.py
```

## Optional external enhancements

For explicit Orca requests, GBStack uses `orchestration` for dispatch and
`orca-cli` for worktrees, terminals, or one-way handoffs when the relevant
skill is installed. Otherwise it follows the usual workflow for the request.
It uses `long-answer-html` to format final reports when installed and replies
normally when that skill is unavailable.
