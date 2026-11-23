# GBStack

GBStack is a portable agy plugin. It combines the on-demand Swarm Coordinator
rules, the `subagent-first` coordination workflow, and four roles for
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

## Codex profiles

The Coordinator checks Codex before dispatching a Codex worker or reviewer.
`worker-codex` is the standard implementation profile,
`worker-codex-xhigh` is for complex work or rework, and `reviewer-codex` is
read-only. The scripts below run from any working directory and require Bash.

```sh
/path/to/gbstack/scripts/setup-codex.sh --dry-run
/path/to/gbstack/scripts/setup-codex.sh
/path/to/gbstack/scripts/check-codex.sh
/path/to/gbstack/scripts/check-codex.sh --codex-home "/path/to/codex-home" reviewer-codex
```

- `setup-codex.sh` installs all three templates. It skips existing profiles by
  default, `--force` replaces them, and `--dry-run` previews actions without
  creating directories or files.
- `check-codex.sh` checks all three profiles by default or only the positional
  profile names provided. It looks for an executable Codex on `PATH` and then
  at `$HOME/.local/bin/codex`. It requires a successful `codex login status`
  containing `Logged in` and a readable file for each requested profile.
  During the login query it sets `TMPDIR` to the resolved Codex home so release
  builds skip creating temporary PATH aliases. Codex may print a warning
  about the skipped aliases even when the check succeeds.
- Both scripts accept `--codex-home DIR` and `-h` / `--help`. The default
  directory is `${CODEX_HOME:-$HOME/.codex}`. The check uses that directory
  for both login status and profile lookup.

## Playbooks and engineering skills

The built-in router in [subagent-first](skills/subagent-first/SKILL.md) matches
Swarm work to one of eight [on-demand playbooks](skills/subagent-first/playbooks/).
Read the matching playbook before breaking down the work or writing briefs. If
none applies, follow the standard `subagent-first` workflow.

Ten standalone engineering skills cover code tracing (`how`), design history
(`why`), system explanation (`teach`), architecture (`architect`), change
impact (`blast-radius`), design critique (`interrogate`), editing (`unslop`),
context recovery (`ketchup`), course correction (`correct`), and requirements
refinement (`sharpen`). Use them on their own or reference them in Swarm briefs.

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
├── rules/
│   └── swarm-addon.md
├── scripts/
│   ├── check-codex.sh
│   └── setup-codex.sh
├── skills/
│   ├── architect/
│   │   └── SKILL.md
│   ├── blast-radius/
│   │   └── SKILL.md
│   ├── correct/
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
└── templates/
    └── codex/
        ├── worker-codex.config.toml
        ├── worker-codex-xhigh.config.toml
        └── reviewer-codex.config.toml
```

## Optional external enhancements

For explicit Orca requests, GBStack uses `orchestration` for dispatch and
`orca-cli` for worktrees, terminals, or one-way handoffs when the relevant
skill is installed. Otherwise it follows the usual workflow for the request.
It uses `long-answer-html` to format final reports when installed and replies
normally when that skill is unavailable.
