# Network Lab MCP

Network Lab MCP provides a lightweight "AI Network Engineer Layer" designed to
maximize the reasoning capabilities of AI in network lab environments.

It is not a fixed test-automation tool. It is a thin foundation that lets
Claude Code (or any other MCP client) understand a lab's topology, follow
common operating principles, understand the current task, draw on reusable
reference knowledge, and reach lab devices through a real terminal — while
Claude Code itself does all of the network engineering reasoning.

This repository includes **Step 1** (the MCP server and terminal
foundation), **Step 2** (an IOS XR-compatible human-facing CLI, launched via
`./run_cli.sh`, for creating/editing lab definitions through a
candidate/commit model), and **Step 2.5** (separating private device access
from safe topology data, reworking the CLI's configuration model around
that separation — see [Configuration model](#configuration-model) below —
completing the IOS XR-style navigation model (`root`/`exit`/`end`, `commit`
staying in the current mode, uncommitted-changes-only `show
configuration`), explicitly selecting access-info in running-config, and
adding single-hop OpenSSH ProxyJump support via access-info `jump_hosts`),
and **Step 3** (IOS XR + LLDP topology discovery via `discover topology`,
persistent terminal session logging, and `show logging`). See
[Step 3: IOS XR + LLDP topology discovery](#step-3-ios-xr--lldp-topology-discovery)
and [Current limitations](#current-limitations).

## Configuration model

Five kinds of lab data are deliberately kept separate:

| Concept | Role | Exposed to Claude? |
|---------|------|---------------------|
| **running-config** | *WHICH* topology/scenario/references MCP currently uses — a **selection**, stored in `lab/settings.yaml` | Indirectly (drives which topology/scenario/references are read) |
| **access-info** | *HOW TO ACCESS* devices — private connection data (address/transport/port/username/password), `lab/access-info/*.yaml` | **Never** |
| **topology** | *WHAT EXISTS / HOW IT IS CONNECTED* — safe logical devices, device type, links, `lab/topologies/*.yaml` | Yes, via `get_active_topology()` |
| **scenario** | *WHAT TO DO* for the current task, `lab/scenarios/*.yaml` | Yes, via `get_execution_instructions()` |
| **reference** | Reusable, validated knowledge, `lab/references/*.yaml` | Yes, via `get_execution_instructions()` |

Concretely:

```
access-info  = HOW TO ACCESS DEVICES     = private, never sent to Claude
topology     = WHAT EXISTS / CONNECTIVITY = safe, sent to Claude
scenario     = WHAT TO DO                 = sent to Claude
reference    = REUSABLE KNOWLEDGE         = sent to Claude
running-config = WHICH topology/scenario/references MCP uses right now
```

`running-config` is a **selection**, not a definition: it never contains
device data itself, only the names of the topology/scenario/references
currently in effect. Topology/access-info/scenario/reference are
**definitions**: each one is a named, independently authored/edited YAML
document. Editing a definition (e.g. `topology lab1` in the CLI) never
changes which definition MCP currently uses; only committing a
`running-config` change does that. This split replaced an earlier Step 2
model where `topology <name>`/`scenario <name>`/`reference <name>` directly
changed the active selection — that old selector semantics no longer
exists anywhere in this CLI.

Also see [Responsibility model](#responsibility-model), which places these
five alongside Principles, Terminal, Claude Code, and Workspace.

## Responsibility model

| Concept        | Role                    | Meaning |
|----------------|-------------------------|---------|
| **running-config** | SELECTION | Which topology/scenario/references MCP currently uses (`lab/settings.yaml`) |
| **Topology**   | WHERE / WHAT EXISTS     | Safe logical devices, device type, and links — no private access data |
| **access-info**| PRIVATE DEVICE ACCESS   | Address/transport/port/username/password — never exposed to Claude |
| **Principles** | HOW TO BEHAVE           | Common operating rules that apply to every scenario |
| **Scenario**   | WHAT TO DO              | What must be accomplished for the current task |
| **References** | REUSABLE KNOWLEDGE      | Reusable validated guidance, operational knowledge, known values, and lab-specific know-how |
| **Terminal**   | ACTION INTERFACE        | How Claude interacts with lab devices |
| **Claude Code**| REASONING               | Determines how to accomplish the task |
| **Workspace**  | TASK ARTIFACT AREA      | Where Claude stores evidence, analysis, configurations, validation results, designs, and reports |

Network Lab MCP deliberately keeps network-engineering judgment out of the MCP
server itself. It exposes lab knowledge and terminal access; Claude Code
decides what to do with them. The Step 2 human CLI adds a further role,
**Human Configuration / Control Interface**: it creates/edits lab
definitions and the running-config selection through a candidate/commit
model, but it is not itself a network-engineering reasoning engine either.
See [docs/architecture.md](docs/architecture.md) for more detail, including
the device-access resolution flow.

## Step 1 capabilities

- A stdio MCP server (`network-lab-mcp`) exposing exactly seven tools:
  `get_active_topology`, `get_execution_instructions`, `terminal_open`,
  `terminal_send`, `terminal_read`, `terminal_list`, `terminal_close`.
- Lab data (running-config, topology, access-info, principles, scenario,
  references) loaded from YAML and re-read from disk on every relevant tool
  call — no server restart needed after a CLI `commit`.
- A dedicated tmux environment (separate socket) used as the source of truth
  for terminal session lifetime, structurally separating production device
  sessions from local validation-only sessions.
- SSH and Telnet access to lab devices via the operating system's own `ssh`
  and `telnet` binaries, driven the way a human would: read the terminal,
  decide what to send, send it.

## Step 2 / 2.5 capabilities: the Human Configuration CLI

`./run_cli.sh` launches an interactive, IOS XR-compatible CLI for
creating/editing lab **definitions** (topology, access-info, scenario,
reference) and the **running-config selection**, all through a
**candidate -> commit** model — the same responsibility split IOS XR uses
for its own configuration mode. It is a human configuration/control plane,
not a network-engineering reasoning engine, and it never talks the MCP
stdio protocol.

- **Modes**: EXEC (`network-lab#`), global configuration
  (`network-lab(config)#`), running-config selection
  (`network-lab(config-running)#`), topology definition
  (`network-lab(config-topology-<name>)#` / nested
  `network-lab(config-device-<name>)#` for safe device metadata),
  access-info definition (`network-lab(config-access-info-<name>)#` /
  nested `network-lab(config-access-device-<name>)#` for private connection
  fields), scenario definition (`network-lab(config-scenario-<name>)#`),
  and reference definition (`network-lab(config-reference-<name>)#`). See
  [docs/cli_reference.md](docs/cli_reference.md) for the full mode/command
  reference.
- **`topology`/`access-info`/`scenario`/`reference <name>` create or edit a
  definition** — an existing name loads it as a candidate, a new name
  starts a fresh one; neither ever changes what MCP currently uses. Only
  `running-config` mode's `topology`/`scenario`/`reference <name>` (and `no
  reference <name>`) change the running-config candidate's selection.
- **Candidate configuration**: entering `configure` snapshots committed
  running-config into a settings candidate; opening a definition
  (topology/access-info/scenario/reference) loads (or creates) a definition
  candidate. At most one definition is open at a time — opening a different
  one while the current one is dirty is blocked, mirroring the topology
  case-only collision safeguard's caution around implicit creation. Nothing
  is written to disk until `commit`.
- **`clear` replaces `abort`**: discards every uncommitted change in the
  current configure session — the running-config candidate and the open
  definition candidate together — restoring committed state, without
  returning to EXEC. If the definition being cleared was brand new (never
  committed), it is discarded entirely rather than reset to empty, and the
  CLI steps back to the nearest still-valid parent mode if the current
  submode's target no longer exists after the revert.
- **External YAML editor** (`edit`, in topology/scenario/reference
  definition mode): opens the candidate in `$VISUAL`, then `$EDITOR`, then a
  `vim` fallback, via a secure temporary `.yaml` file — never the committed
  file directly. A non-zero editor exit or invalid YAML leaves the
  candidate untouched; `commit` is still required to persist the edit.
  access-info uses structured CLI editing only in this phase (no `edit`).
- **IOS XR-style interaction**, all driven by one command grammar (the single
  source of truth in `cli/grammar.py`, see
  [docs/cli_reference.md](docs/cli_reference.md) for the full reference):
  unique fixed-keyword abbreviation, Tab/Ctrl-I completion, context-sensitive
  `?` (bare, partial-token, and next-token forms, including "select or
  create" identifiers that list existing names alongside a creation hint,
  and a `<cr>` marker), ambiguous/incomplete/invalid-input detection with an
  IOS XR-style caret, in-process command history (never written to disk,
  and a password-setting command is never retained in it even in memory),
  IOS XR-style line editing, and safe Ctrl-C (cancels only the current input
  line) / Ctrl-D (EOF; blocked while uncommitted changes exist) behavior.
  Repeated `?` leaves a `prompt + buffer + ?` transcript line in scrollback
  before each help block, matching a real terminal.
- **Multi-line configuration paste**: pasting a multi-line block (e.g.
  copied from a `show` output) runs each physical line through the same
  grammar/handlers as manually typed input, in order, so mode-changing
  lines (`device`/`exit`/`root`/`end`/`clear`/`commit`/...) take effect
  before the next line runs. Leading indentation is stripped; processing
  stops at the first invalid line, with earlier lines left in the
  candidate. A standalone `!` separator line has a narrow, explicit
  meaning bounded to access-info's own three modes (exactly one level up,
  mirroring the renderer's own block-closing convention, so a rendered
  access-info block pastes back without manually inserting `exit`
  between sibling blocks); everywhere else, including global
  configuration mode and EXEC, it is a safe no-op. See
  [docs/cli_reference.md](docs/cli_reference.md#multi-line-configuration-paste).
- **Fixed CLI keywords are case-insensitive**; **object identifiers —
  topology, scenario, reference, access-info, and device names — are
  case-sensitive** and are never silently case-folded, including in dynamic
  completion.
- **Case-only topology-name collision safeguard**: if `topology <name>` does
  not exactly match an existing topology but differs from one only by
  letter case, the CLI asks for explicit confirmation before creating a
  distinct topology, rather than silently opening the existing one or
  silently creating a look-alike. This is a narrow safety check, not fuzzy
  name matching.
- **Step 1 validator reuse**: topology/access-info/device.type validation on
  commit reuses `network_lab_mcp.lab.validate_topology_data()` /
  `validate_access_info_data()` / `normalize_device_type()` — the CLI does
  not maintain a duplicate set of validation rules.
- **Minimal, targeted writes**: `commit` writes only the definition file(s)
  that actually changed semantically, then `lab/settings.yaml` last (since a
  running-config selection may point at a definition just created in the
  same commit); an unchanged definition that was merely opened is never
  rewritten, and a no-op commit writes nothing at all.
- **Commit stays in the current mode**: `commit` only saves the candidate;
  IOS XR-style navigation (`root`, `exit`, `end`) is what moves you between
  modes. `root` jumps straight to global configuration mode from any nested
  submode, preserving candidate state (never commits, never clears); `exit`
  moves exactly one configuration level up; `end` is a guarded jump to
  EXEC, blocked while any candidate scope is dirty, exactly like `exit`/`end`
  always were.
- **`show`/`show configuration` inside a configuration mode display
  uncommitted changes only** — a bounded, pragmatic delta (field-level for
  structured scalar fields such as access-info/device/jump-host fields,
  the whole small document for open-schema scenario/reference content),
  never a full dump of the candidate. Bare `show` (no argument) is the same
  command as `show configuration` there; entering a brand-new, still-empty
  object produces no output. `show running-config` is unaffected: it always
  shows the full current committed state for the current context (see
  ["Configuration show semantics"](#configuration-show-semantics) below).
- **Password safety**: passwords are stored in access-info YAML in plain
  text (this is a lab tool, not a secret manager). Explicit local CLI
  display (`show`/`show configuration`/`show running-config` for an
  access-info device or jump host) shows the password in clear text —
  see ["Password display policy"](#password-display-policy) — but it is
  never offered as a completion candidate, never retained in this
  process's in-memory history, and never reaches MCP tool results, logs,
  or error messages.
- **Committed-state boundary**: the MCP server only ever reads committed
  `lab/settings.yaml`, `lab/topologies/*.yaml`, and (indirectly, for
  terminal access) `lab/access-info/*.yaml`; candidate configuration is
  memory-only and invisible to Claude Code until `commit` succeeds, at which
  point it becomes visible on the very next MCP tool call — no MCP server
  restart is needed.

## Configuration show semantics

`show running-config` and `show configuration` are both scoped to the
*current CLI context*:

- **EXEC, global configuration, and `running` mode**: `show running-config`
  is the committed MCP running-config selection (access-info/topology/
  scenario/reference names) — this is the one meaning that predates
  definitions having their own candidates. `show configuration` is the
  candidate version of the same thing.
- **A definition mode** (`topology`/`access-info`/`scenario`/`reference`,
  or their nested device/jump-host submodes): both commands are scoped to
  *that one object* instead — `show running-config` is its full committed
  state (re-read fresh from disk; empty if never committed), `show
  configuration` (and bare `show`) is its *uncommitted changes only*.

```
network-lab(config)# access-info test_lab
network-lab(config-access-info-test_lab)# device R1
network-lab(config-access-device-R1)# type iosxr
network-lab(config-access-device-R1)# address 192.0.2.11
network-lab(config-access-device-R1)# show running-config
network-lab(config-access-device-R1)# show
access-info test_lab
 device R1
  type iosxr
  address 192.0.2.11
 !
!
```

(`show running-config` printed nothing — "empty" means no output line at
all — because `test_lab` was never committed.) See
[docs/cli_reference.md](docs/cli_reference.md#show-running-config-vs-show-configuration)
for the full reference, including the field-level delta rules for
structured scalar configuration and the bounded whole-document fallback
used for scenario/reference.

### `show running-config <definition-type>` (EXEC only)

A convenience read-only dereference of the active selections bare `show
running-config` already lists, built on the exact same committed
renderers as definition mode: `show running-config access-info` /
`topology` / `scenario` show that one active definition's committed
content (no `<name>` -- each has at most one active definition). `show
running-config reference` shows *every* active reference in committed
order (multi-select, unlike the other three); `show running-config
reference <name>` shows just one, and only if it is currently active --
a reference stored on disk but not selected is rejected, not looked up.
See [docs/cli_reference.md](docs/cli_reference.md#show-running-config-definition-type)
for the full reference.

## Architecture overview

```
Claude Code                          Human Operator
    |                                     |
Network Lab MCP (this repository)   ./run_cli.sh (this repository)
    |                                     |
dedicated tmux environment           Candidate configuration -> commit
(socket: network-lab-mcp)                 |
    |                                Committed lab YAML
ssh / telnet                              |  (running-config, topology,
    |                                     |   access-info, scenario, reference)
Lab Devices                          (read by Network Lab MCP above)
```

See [docs/architecture.md](docs/architecture.md) for the full picture,
including how running-config, access-info, topology, Principles, Scenario,
References, Terminal, Claude Code, and Workspace relate to each other, the
device-access resolution flow `terminal_open()` follows, how the production
and validation terminal session namespaces are kept structurally separate,
and how the CLI's candidate/commit model relates to the committed-state
boundary the MCP server reads from.

## Directory structure

```
network-lab-mcp/
├── pyproject.toml
├── README.md
├── .gitignore
├── run_cli.sh                 # Step 2 human CLI launcher
│
├── src/
│   └── network_lab_mcp/
│       ├── __init__.py
│       ├── mcp_server.py      # stdio MCP server, defines the 7 tools
│       ├── lab.py             # running-config/topology/access-info/scenario/reference loading
│       ├── terminal.py        # tmux session management, ssh/telnet launch
│       │
│       └── cli/                       # Step 2 human-facing CLI
│           ├── __init__.py
│           ├── main.py                # REPL, prompt rendering, key bindings, dispatch
│           ├── config.py              # candidate configuration, dirty state, commit/clear
│           ├── grammar.py             # command grammar single source of truth
│           └── editor.py              # external ($VISUAL/$EDITOR/vim) YAML editor support
│
├── lab/
│   ├── settings.example.yaml  # tracked template (running-config)
│   ├── settings.yaml          # local only, gitignored (running-config)
│   ├── principles.yaml
│   │
│   ├── access-info/
│   │   ├── sample.yaml        # tracked; fictional sample only
│   │   └── ...                # any other file here is local/private, gitignored
│   │
│   ├── topologies/
│   │   └── sample.yaml        # tracked; safe logical data, documentation-only addresses
│   │
│   ├── scenarios/
│   │   └── sample.yaml
│   │
│   └── references/
│       └── sample.yaml
│
└── docs/
    ├── architecture.md
    ├── mcp_tools.md
    ├── cli_reference.md
    └── scenario_format.md
```

`lab/access-info/sample.yaml` and `lab/topologies/sample.yaml` are
independent sample files that happen to share a name purely by convention
(both are the one canonical public sample for their respective concept) —
see
["Access-info and topology filenames are not linked"](#access-info-and-topology-filenames-are-not-linked).

## Installation model

### Supported: local editable installation

Step 1 supports exactly one deployment model: a local repository checkout,
installed with `pip install -e .`. The repository checkout owns the `lab/`
directory, and the MCP server resolves it relative to its own source
location — never relative to the current working directory of whatever
process launched it.

```bash
cd ~/work/network-lab-mcp

python3 -m venv .venv
source .venv/bin/activate

pip install -e .
```

Claude Code itself is normally started from a separate task workspace, e.g.:

```bash
cd ~/work/customer-lab-investigation
claude --permission-mode bypassPermissions
```

Network Lab MCP does not depend on that working directory. One Network Lab
MCP checkout owns exactly one lab root, and that lab root can contain
multiple topologies, access-info definitions, scenarios, and references;
`lab/settings.yaml` (running-config) selects which topology/scenario/
references are currently in effect.

### Not supported in Step 1: non-editable / wheel installation

`pip install .`, installing from a built wheel, or `pip install
network-lab-mcp` from a package index are **not supported** in Step 1.
`lab/` is repository-local operational data, not a Python package resource,
so a non-editable install has no lab directory to find. Making that work
would require packaging changes (e.g. `importlib.resources`, an external
writable config directory, environment-variable-based lab-root overrides)
that are explicitly out of scope for Step 1. This may be reconsidered in a
later step.

## The `network-lab-mcp` command

`pyproject.toml` defines a console script:

```toml
[project.scripts]
network-lab-mcp = "network_lab_mcp.mcp_server:main"
```

Running `network-lab-mcp` starts the stdio MCP server. It takes no arguments
and does not read the current working directory to find lab data.

## Registering with Claude Code

Register the server once, from anywhere, after installing it into an
activated environment:

```bash
claude mcp add --scope user --transport stdio network-lab -- network-lab-mcp
```

(Verified against Claude Code CLI 2.1.277's `claude mcp add --help`; if a
newer Claude Code version changes this syntax, follow its own `--help`
output instead of this snippet.)

`--scope user` registers the server for the current user across all
projects. `network-lab-mcp` must be resolvable on `PATH` at the time Claude
Code launches it — for example, by installing it into an environment that is
active in your shell, or by using that environment's absolute path in place
of `network-lab-mcp`.

## Lab directory concepts and setup

- `lab/settings.example.yaml` is the tracked template for running-config.
- `lab/settings.yaml` is your local, machine-specific running-config
  selection. It is gitignored. Create it once:

  ```bash
  cp lab/settings.example.yaml lab/settings.yaml
  ```

- Changing which topology/scenario/references MCP uses, or
  creating/editing topology/access-info/scenario/reference definitions, can
  be done either by directly editing the corresponding YAML files, or
  through the Step 2 human CLI (`./run_cli.sh`) described below and in
  [docs/cli_reference.md](docs/cli_reference.md). Either way, changes take
  effect on the next MCP tool call; the MCP server does not need to be
  restarted, because lab YAML is re-read from disk on every relevant call.

## Running the Human Configuration CLI

```bash
./run_cli.sh
network-lab#
```

This launches the Step 2 IOS XR-compatible CLI in the same activated
environment used for `pip install -e .` (there is no separate console
script for it; it is run as a module by `run_cli.sh`). It edits
`lab/settings.yaml` (running-config) and topology/access-info/scenario/
reference definition YAML directly through a candidate/commit model — see
[Step 2 / 2.5 capabilities](#step-2--25-capabilities-the-human-configuration-cli)
above and [docs/cli_reference.md](docs/cli_reference.md) for the full
command reference. It is a separate process from `network-lab-mcp`; you can
run the CLI to change lab configuration and the MCP server (if already
running for Claude Code) will pick up a successful `commit` on its next
tool call.

### Sample topology and access-info

`lab/topologies/sample.yaml` and `lab/access-info/sample.yaml` are
tracked in git. The topology holds only safe logical data (devices, device
type, links); the access-info definition holds the matching fictional
connection data, using only documentation-only addresses from the RFC 5737
`192.0.2.0/24` range. **These addresses are not reachable and must not be
used as real connectivity targets.** The samples exist to validate YAML
loading, MCP structured output, device-access resolution, and
device-name/session-name mapping — not to be a real lab.

### Access-info and topology filenames are not linked

Network Lab MCP never infers "this access-info file belongs to this
topology" from filenames, matching or not — running-config's explicit
`access-info <name>` / `topology <name>` selections are the only
association; see
["Device access resolution"](#device-access-resolution) below. This holds
regardless of which access-info files happen to exist locally or what
they're named; only `lab/access-info/sample.yaml` is tracked in git (see
["Git safety design"](#git-safety-design) below), but any other
`lab/access-info/*.yaml` a user creates locally is just as valid a
selection target.

### Topology device-name validation

Device names in a topology must map safely and unambiguously to an internal
terminal session name. `lab.py` validates, at load time, that:

- each device name is a non-empty string using a safe character set
  (letters, digits, `-`, `_`);
- no two device names can resolve to the same terminal session name.

This validation does **not** reject device names merely because they begin
with `validation-` (e.g. `validation-router` is a perfectly legitimate
device name) — see
["Structurally separate session namespaces"](#structurally-separate-session-namespaces)
below for why that is safe.

### Topology holds only safe logical data

Topology YAML may contain `name`, `description`, `devices` (each with an
optional `type`), and `links` — nothing else. `address`, `transport`,
`port`, `username`, and `password` are rejected outright by
`lab.validate_topology_data()` if present, whether the file was written by
the CLI or edited by hand: those fields belong in an access-info
definition instead, because topology is exposed to Claude via
`get_active_topology()` and access-info never is.

### Device type enum

A device's optional `type` field, when present (in either topology or
access-info), must be one of:

- `iosxr` — Cisco IOS XR
- `iosxe` — Cisco IOS XE
- `nxos` — Cisco NX-OS
- `host` — Generic host / endpoint

`lab.normalize_device_type()` is the single validation primitive for this
enum, applied by `lab.py` at topology and access-info load/write time (so a
manually edited YAML file with an unsupported `type` is rejected in either
place) and by the Step 2 CLI's `type` argument under both the topology
device submode and the access-info device submode (so `type ?`/Tab only
ever offer these four values, and an unambiguous abbreviation like `type
nx` normalizes to `nxos`, or `type h` to `host`).

This enum is also how Step 3's `discover topology` selects its targets
from the selected access-info's devices: `iosxr` is the only currently
supported Discovery target (LLDP only, see
[Step 3](#step-3-ios-xr--lldp-topology-discovery) below); `host` is a
normal registered topology node for which discovery is intentionally
skipped, not an "unsupported type" error, and `iosxe`/`nxos` are likewise
skipped (not implemented yet) rather than failing a mixed access-info
definition.

Topology and access-info store `type` independently (they are separate
files, possibly authored at different times); terminal access validates
them against each other only when both are present — see
["Topology/access-info device.type consistency"](#topologyaccess-info-devicetype-consistency)
below.

### Sample scenario and references

`lab/scenarios/sample.yaml` and `lab/references/sample.yaml` are minimal
tracked examples used to validate that `get_execution_instructions()` can
load and combine principles, an active scenario, and active references. The
scenario format is intentionally not finalized in Step 1 — see
[docs/scenario_format.md](docs/scenario_format.md). Both can be
authored/edited from the CLI via `scenario <name>` / `reference <name>` and
`edit`.

## Git safety design

This repository is meant to be shared publicly, but real access-info YAML
contains device names, management addresses, usernames, passwords, and
other private lab information. Rather than relying on documentation alone,
`.gitignore` provides a default technical guard:

- `lab/settings.yaml` (your local running-config selection) is gitignored.
- `lab/settings.example.yaml` is tracked.
- `lab/topologies/sample.yaml` is tracked (safe logical data only).
- `lab/access-info/sample.yaml` is tracked (fictional data only) — the
  **only** access-info YAML tracked in git.
- Every other file under `lab/topologies/*.yaml` and `lab/access-info/*.yaml`
  is gitignored by default, including any real access-info file a user
  creates locally (e.g. via the CLI's `access-info <name>` /
  `jump-host <name>` submodes) — regardless of its name.

Concretely:

```gitignore
lab/settings.yaml
lab/topologies/*.yaml
!lab/topologies/sample.yaml
lab/access-info/*.yaml
!lab/access-info/sample.yaml
```

This was validated by creating `lab/access-info/private_lab.yaml` and
`lab/topologies/private_lab.yaml` and confirming that a plain `git add .`
does not stage either, while `lab/access-info/sample.yaml`,
`lab/topologies/sample.yaml`, and `lab/settings.example.yaml` do get
staged normally.

This is **not** a complete security boundary — it is a default that lowers
the chance of accidentally committing real credentials or private lab data
to a public repository. Treat any access-info file that leaves this
repository as sensitive regardless of what git tracks. Topology YAML no
longer carries private access fields at all, so it is a much lower-risk
file even before considering `.gitignore`.

### Credential handling

Real access-info YAML stores device usernames and passwords directly (this
is a lab tool; it does not introduce a separate secret store). Credentials
are used only to drive interactive terminal login. Network Lab MCP:

- never logs passwords or `terminal_send()` input text;
- never echoes `terminal_send()` input text back in tool responses;
- never includes credentials in generated documentation or error messages
  (including the device-access-ambiguity and type-mismatch errors below);
- never persists credentials into any separate runtime database (there isn't
  one — tmux is the only session state);
- never returns access-info from any MCP tool: `get_active_topology()`
  returns only the safe topology, and `get_execution_instructions()` never
  includes it either.

The Step 2 human CLI applies the same principle to lab configuration
editing, with one deliberate exception documented below: a device or
jump-host `password` is stored in plain text in access-info YAML (as in
Step 1 — this is a lab tool, not a secret manager), is never offered as a
Tab/`?` completion candidate, and is never retained in the CLI's own
in-memory command history. External-editor support (`edit`) is not offered
for access-info in this phase, so credentials are never written to an
external editor's temporary file. See
[docs/cli_reference.md](docs/cli_reference.md) for details.

### Password display policy

Network Lab MCP is primarily a lab tool, so **explicit local CLI
configuration display** — `show running-config` / `show configuration` /
bare `show` for an access-info device or jump host — shows `password` in
**clear text**, not masked:

```
network-lab(config-access-device-R1)# show running-config
access-info test_lab
 device R1
  type iosxr
  address 192.0.2.11
  transport ssh
  port 22
  username cisco
  password cisco
 !
!
```

This is the **only** place a password is ever shown in clear text. Every
other boundary is unchanged and unweakened:

- MCP tool results (`get_active_topology()`, `get_execution_instructions()`,
  every `terminal_*()` return value) never include it.
- Logs, exceptions, and every `%`-prefixed error message never include it
  (including the access-info-not-found/ambiguous and type-mismatch errors
  below).
- `?` help and Tab completion never reveal or offer it as a candidate.
- The CLI's in-memory command history never retains a password-setting
  command, even abbreviated.

## Device access resolution

`terminal_open(device)` receives only a logical device name from Claude —
never an address, username, or password. Network Lab MCP resolves the
private connection details itself:

1. Read the committed running-config and resolve the active topology.
2. Verify the device exists in the active topology.
3. Resolve the running-config's *selected* access-info
   (`active_access_info`) — **fail closed** if none is selected
   (`% No access-info is selected in running-config.`), or if the selected
   definition does not exist on disk.
4. Load **only that one** access-info definition and look up the device in
   it — **fail closed** (`% Device '<device>' is not present in
   access-info '<name>'.`) if it is absent. No other access-info file is
   ever searched.
5. If both the topology and the resolved access-info specify `type`,
   normalize both through the shared `DEVICE_TYPES` enum and compare —
   **fail closed** on a mismatch.
6. If the device has an optional `jump_host` reference, resolve it within
   the same access-info definition and attach it for a single-hop OpenSSH
   ProxyJump connection (see ["Single-hop SSH jump hosts"](#single-hop-ssh-jump-hosts-proxyjump)
   below); otherwise connect directly, exactly as before.
7. Only then does the existing tmux/ssh/telnet path run.

Selecting which access-info definition step 3 reads is done through
`running-config`'s `access-info <name>` / `no access-info` (see
["Selecting access-info in running-config"](#selecting-access-info-in-running-config)
below) — the same candidate/commit model as topology/scenario/reference
selection, and just as invisible to `terminal_open()` until committed.

### Selecting access-info in running-config

```
network-lab# configure
network-lab(config)# running-config
network-lab(config-running)# access-info test_lab
network-lab(config-running)# commit
Commit complete.
network-lab(config-running)# end
network-lab# show running-config
!
 access-info
  test_lab
!
 topology
  sample
!
 scenario
  sample
!
 reference
  sample
!
```

`no access-info` removes the selection from the candidate (omission, not a
sentinel value like `"none"`); a `settings.yaml` written before this field
existed, or with no access-info selected, is a legitimate, fail-closed
state — `get_active_access_info_name()` treats a missing field as "no
selection", not an error.

### Access-info lookup is no longer global

Earlier in Step 2.5, access-info lookup searched every committed
`lab/access-info/*.yaml` file for a matching device ID and failed closed on
ambiguity if more than one file contained it. Now that running-config
explicitly selects **one** access-info definition, that global search is
gone entirely (not merely bypassed) — only the selected definition is ever
read, so the same device ID may safely appear in other, unselected
access-info files:

```
lab/access-info/lab_a.yaml   R1
lab/access-info/lab_b.yaml   R1
```

With `active_access_info: lab_a`, `terminal_open("R1")` resolves
`lab_a`'s `R1` only; selecting `lab_b` instead resolves `lab_b`'s `R1`
instead. Neither a matching topology filename, edit recency, nor
alphabetical order is ever used to choose between access-info files —
there is exactly one selection, and it is explicit.

### Single-hop SSH jump hosts (ProxyJump)

access-info can declare reusable `jump_hosts`, each a generic endpoint
(`type: host` — never a network-device type) reached over SSH:

```yaml
name: test_lab

jump_hosts:
  jump1:
    type: host
    address: 192.0.2.10
    transport: ssh
    port: 22
    username: cisco
    password: cisco

devices:
  R1:
    type: iosxr
    address: 192.0.2.11
    transport: ssh
    port: 22
    username: cisco
    password: cisco
    jump_host: jump1
```

A device's optional `jump_host` field references one jump host by name
within the *same* access-info definition. When resolving `R1` above,
`terminal_open()` launches native OpenSSH with `-J` (conceptually `ssh -J
cisco@192.0.2.10:22 -p 22 cisco@192.0.2.11`) instead of connecting
directly — there is no shell-hop automation (no "SSH to the jump host,
wait for its shell prompt, then SSH again"), just OpenSSH's own ProxyJump
handling one SSH connection tunneled through another. The tmux pane still
only ever shows one interactive session to read/send against, exactly like
a direct connection.

Constraints, all enforced by `lab.validate_access_info_data()` (so a
manually edited, invalid committed file fails closed the same way a
rejected CLI commit would):

- a jump host's `type` must resolve to exactly `host` (reusing
  `normalize_device_type()`/`DEVICE_TYPES`, so `HOST`/`Host`/`h` still
  work, but `iosxr`/`iosxe`/`nxos` are rejected);
- a jump host's `transport`, if set, must be `ssh` (ProxyJump is SSH-only);
- a device's `transport` must also be `ssh` when it references a
  `jump_host` — a telnet device can never use one;
- single-hop only: a jump host has no `jump_host` field of its own: nesting
  one jump host behind another is not supported.

A device with no `jump_host` continues to connect directly, unchanged.
Structured CLI support: `access-info <name>` gains `jump-host <name>`
(`network-lab(config-access-jump-host-<name>)#`, with the same
type/address/transport/port/username/password fields as a device), and
`access-info <name>`'s device submode gains `jump-host <name>` (a reference,
with Tab/`?` completion over the access-info definition's own jump host
names) and `no jump-host`.

`access-info <name>` mode also has `no device <name>` / `no jump-host
<name>`, removing the whole object from the candidate (never committed YAML
directly, never auto-committed). Deletion is not cascading: removing a
jump host that a device still references leaves that reference dangling in
the candidate, and `commit` fails on it, the same way it fails on any other
invalid candidate, until the reference is fixed (`no jump-host` inside the
device's own mode) or the jump host is restored. See
[docs/cli_reference.md](docs/cli_reference.md) for the full command table.

### Topology/access-info device.type consistency

Topology and access-info are independent, separately authored definitions,
so nothing stops them from disagreeing about the same device's platform.
`terminal_open()` performs a lightweight runtime check when resolving a
device — not a general cross-file consistency framework — and fails closed
if both sides specify `type` and, once normalized through the shared
`DEVICE_TYPES` enum, they disagree:

```
% Device type mismatch for 'R1' between topology and access information.
```

If either side leaves `type` unset (already allowed), there is nothing to
compare and resolution proceeds normally — this check does not make `type`
mandatory anywhere it previously was not. Credential values are never
included in this error.

## Dedicated tmux environment

All Network Lab MCP terminal sessions run on a dedicated tmux server, reached
with `tmux -L network-lab-mcp ...`, separate from any tmux environment you
use interactively. This keeps `terminal_list()`/`terminal_close()` safe from
interfering with unrelated sessions, and lets Network Lab MCP configure
tmux options (like scrollback history) without touching your own tmux
setup.

### Structurally separate session namespaces

Production topology-device sessions and local validation-only sessions live
in structurally distinct namespaces, distinguished by a fixed prefix — never
by searching for the substring `validation` inside a name:

```
network-lab-device-<device-id>        production topology device sessions
network-lab-validation-<validation-id> local validation-only sessions
```

So a topology device literally named `validation-router` maps to the
production session `network-lab-device-validation-router`, which is *not*
classified as a validation session; it is exposed by `terminal_list()` like
any other device, and `terminal_close(device="validation-router")` can only
ever target that production session, never anything in the
`network-lab-validation-*` namespace.

### tmux persistence

Terminal sessions live in tmux, independent of the MCP server process. The
server does not create a session registry of its own, and it never destroys
a tmux session on exit. A restarted MCP server rediscovers and reuses any
existing managed session instead of creating a duplicate.

## stdio / stdout rule

The MCP server communicates over stdio, and stdout is reserved for MCP
protocol traffic. It never uses `print()`, prints no startup banner, and
sends all diagnostic logging to stderr.

## Local terminal validation policy

The sample topology's addresses are documentation-only and not connectivity
targets, so device-independent terminal mechanics (tmux session creation and
reuse, `terminal_send()` ordering, scrollback capture, special-key handling,
session persistence) are validated locally against a safe local process
(e.g. `cat` running in a `network-lab-validation-*` session), reusing the
exact same internal tmux session-management primitives that
`terminal_open()`/`terminal_send()`/`terminal_read()`/`terminal_list()`/
`terminal_close()` use in production. This local validation harness is
internal only: it does not add a public "shell" transport, and it does not
add a new public MCP tool.

## Current limitations

- Topology discovery (Step 3) is IOS XR + LLDP only: no CDP, no IOS XE/
  NX-OS discovery, no SNMP/NETCONF/RESTCONF, and no generic discovery
  plugin framework. See
  ["Step 3: IOS XR + LLDP topology discovery"](#step-3-ios-xr--lldp-topology-discovery)
  below.
- No `no topology <name>` (topology deletion) from the CLI, and no
  automatic stale-link pruning after Discovery.
- Login to a device via the public `terminal_open()`/`terminal_send()`/
  `terminal_read()` path is interactive, not automated -- only Discovery's
  private bootstrap path (see below) automates login, and only for its own
  temporary sessions.
- Non-editable/wheel installation is not supported.
- Telnet transport mechanics (binary detection, command construction, launch
  inside the managed tmux path, and output visibility) were validated against
  a local port with no listening Telnet service; a live Telnet device
  interaction was not validated in this environment.
- access-info has no external-editor support in this phase (structured CLI
  editing only), unlike topology/scenario/reference.
- Single-hop OpenSSH ProxyJump only: a jump host cannot itself reference
  another jump host, and only `type: host` / `transport: ssh` jump hosts
  are supported.
- The case-only collision safeguard (`topology <name>`) is mandatory and
  implemented; the equivalent lightweight safeguard for `device <name>` (or
  for access-info/jump-host/scenario/reference names) is not implemented
  (identifiers remain fully case-sensitive regardless).
- Scenario/reference schema is intentionally not fixed yet — only "valid
  YAML, root is a mapping" is enforced (see
  [docs/scenario_format.md](docs/scenario_format.md)).

## Step 3: IOS XR + LLDP topology discovery

Step 3's initial scope is deliberately narrow: **IOS XR devices, LLDP
only**, direct SSH or existing single-hop ProxyJump, driven from the
already-committed `active_access_info`. It never installs/activates a
package, enables LLDP, or changes router configuration; `show cdp
neighbors` is explicitly not part of this phase (the real lab runs
`xr-lldp`, not the CDP package). CDP, IOS XE/NX-OS discovery, SNMP/
NETCONF/RESTCONF, multi-hop jump chains, and a generic discovery/plugin
framework are all out of scope for this phase.

```
committed active_access_info
    -> private bootstrap connection (direct SSH / ProxyJump, reused from
       terminal.py; a structurally separate, temporary session namespace
       -- never reachable through the public terminal_open())
    -> IOS XR login + `show version` / `show running-config` /
       `show lldp neighbors`, captured to a persistent terminal log
    -> IOS XR LLDP parsing -> normalized observations
    -> identity resolution (bounded hostname/alias matching, fails closed
       on anything ambiguous) -> managed vs. unresolved neighbor
    -> link reconciliation (reciprocal dedup, parallel links preserved,
       conflicts reported, never silently resolved)
    -> topology candidate (same candidate/commit/clear system as any
       other topology edit)
    -> `show` / `show configuration` for review -> explicit `commit`
```

- **`discover topology`** (global configuration mode only): reads
  *committed* `active_access_info` (never an uncommitted candidate
  selection), selects its `type: iosxr` devices (`type: host` is skipped,
  not an error; `iosxe`/`nxos` are unsupported and skipped; zero IOS XR
  targets is a hard failure), and requires **all** of them to succeed --
  any login/command/timeout failure fails the whole operation before the
  prior candidate is touched. On success it prints a Discovery summary
  and, if any LLDP neighbor could not be resolved to a managed device,
  an explicit "Unresolved neighbors" section (raw Device ID, observing
  device/interface, remote port, capability) — then enters topology
  configuration mode with the result applied as the candidate, exactly
  like a manually typed `topology <name>`. It never commits and never
  changes `active_topology` itself.
- **Managed vs. unresolved neighbors**: an LLDP neighbor is "managed"
  only if its Device ID resolves *uniquely* to one of the selected
  access-info's own IOS XR devices (exact hostname match, then
  case-normalized match, then a short-name/FQDN-style alias match — never
  a substring search, never inferred from the logical device ID).
  Anything else (e.g. a real external router visible only via LLDP) stays
  unresolved: it is never invented as a managed topology device. Its raw
  evidence is retained for the current `discover topology` run's own
  output and remains reviewable afterwards through the device's own
  persistent terminal log (`show logging <device-id> <log-file>`, since
  the original `show lldp neighbors` output is right there) — there is no
  separate `show discover`/discovery-history command or database; re-running
  `discover topology` produces a fresh normalized result the same way.
- **Link reconciliation**: two devices' reciprocal LLDP observations
  collapse into one topology link (keyed by the unordered pair of
  (device, interface) endpoints, so parallel links on different
  interfaces between the same two routers stay distinct); a one-sided
  observation (only one side ran LLDP) still creates a link; a reciprocal
  pair that disagrees about the interface mapping is reported as a
  conflict and not silently resolved into either interpretation.
- **Existing vs. new target topology**: if a topology already exists
  under the default name, its description and unrelated devices/links are
  preserved — Discovery only adds newly discovered managed devices/links
  (never duplicating an already-present link, and never deleting a link
  merely because this run didn't observe it; stale-link pruning is out of
  scope). If it doesn't exist yet, a brand-new candidate is created, with
  nothing on disk until `commit`.
- **Default topology name**: the selected access-info definition's own
  name (e.g. `active_access_info: test_lab` defaults to `topology
  test_lab`) — this is only Discovery's default *result* name, never a
  runtime requirement; access-info and topology names are still free to
  differ, as everywhere else in this project.
- **Persistent terminal logs**: every device session (production, and
  Discovery's private bootstrap sessions alike) is logged via tmux's own
  `pipe-pane` to `logs/terminal/<device-id>/<session-start>.log`
  (`YYYYMMDDT HHMMSS` session-start timestamp), gitignored. tmux's pane
  remains the runtime session source of truth and `terminal_read()` is
  unchanged; the log is a separate, write-only historical record. Bare
  `show logging` (EXEC only) lists every device's logs newest-first;
  `show logging summary` summarizes every valid device logging directory
  with its eligible log-file count instead (an empty directory counts as
  `0` and is still shown); `show logging <device-id>` lists just one
  device's logs newest-first; `show logging <device-id> <log-file>` shows
  one log's contents — all four are read-only and integrated through the
  same grammar SSOT (`?`, `<cr>`, Tab completion).

  ```
  network-lab# show logging
  Device  Session Start        Log File
  ------  -------------------  --------------------
  R1      2026-09-21 10:32:10  20260921T103210.log
  R2      2026-09-21 10:31:55  20260921T103155.log

  network-lab# show logging summary
  Device  Log Files
  ------  ---------
  R1             12
  R2              8
  ------  ---------
  Total          20

  network-lab# show logging R1
  Session Start        Log File
  -------------------  --------------------
  2026-09-21 10:32:10  20260921T103210.log

  network-lab# show logging R1 20260921T103210.log
  <terminal transcript>
  ```

  `delete logging all` / `<device-id> all` / `<device-id> <log-file>`
  (files only, device directory left in place) and `delete logging all
  directory` / `<device-id> directory` (files, then the now-empty
  directory itself — never `logs/terminal/` — removed non-recursively,
  never `rm -rf`) delete stored logs, reusing the exact same
  eligibility/enumeration as `show logging` — no wildcards, no recursive
  directory deletion, and never a log currently being written: a
  production or Discovery bootstrap session both attach logging to the
  same `logs/terminal/<device-id>/` directory keyed by device name, and
  since no registry records which exact file a live session is writing,
  protection is conservative and device-level — if either kind of session
  exists for a device, none of that device's logs or its directory can be
  deleted until it ends. Every form requires an explicit `[y/N]`
  confirmation (Enter alone safely cancels) that is re-validated right
  after the answer, before anything is deleted, so state that changed
  while the operator was deciding aborts the operation instead of
  silently deleting something different than what was shown; a `delete
  logging ...` line inside a multi-line paste always fails closed rather
  than blocking on, or misreading, the next pasted line as the answer.
  Bulk deletion (`all`) preflights the whole target set first: any active
  device anywhere in scope means nothing at all is deleted. See
  [docs/cli_reference.md](docs/cli_reference.md#delete-logging).

- **Example** (`test_lab` selected as `active_access_info`, already
  containing R1-R4):

  ```
  network-lab# configure
  network-lab(config)# discover topology
  Discovering topology from access-info 'test_lab'...
  Discovery complete.

    Access-info:          test_lab
    IOS XR targets:       4
    Connected:            4
    LLDP observations:    20
    Managed links:        8
    Unresolved neighbors: 2
    Topology candidate:   test_lab

  Unresolved neighbors:
    ASR9001_R1.cisco.com
      R1 GigabitEthernet0/0/0/10 -> GigabitEthernet0/0/0/0 (router)
      R2 GigabitEthernet0/0/0/10 -> GigabitEthernet0/0/0/1 (router)

  network-lab(config-topology-test_lab)# show configuration
  ...
  network-lab(config-topology-test_lab)# commit
  Commit complete.
  network-lab(config-topology-test_lab)# root
  network-lab(config)# running-config
  network-lab(config-running)# topology test_lab
  network-lab(config-running)# commit
  Commit complete.
  ```

  `get_active_topology()` (and every other MCP tool) keeps returning the
  previously active topology until that final explicit running-config
  `topology`/`commit` step — Discovery's own commit only persists the
  topology *definition*, exactly like any other `topology <name>` commit.

- **Real-lab acceptance tests are gated** (never run by a plain `pytest`):

  ```
  NETWORK_LAB_REAL_TESTS=1 pytest tests/test_real_lab_iosxr.py -v --tb=line
  ```

  (`--tb=line`, and never `--showlocals`, so a real device's password
  never ends up in a failure traceback.)

## MCP SDK

Network Lab MCP uses the official
[MCP Python SDK](https://pypi.org/project/mcp/), pinned as `mcp>=2.2,<3` in
`pyproject.toml` (the current stable major version at implementation time).
It uses the SDK's `MCPServer` class (the mcp 2.x name for what was `FastMCP`
in mcp 1.x) over the `stdio` transport. See
[docs/mcp_tools.md](docs/mcp_tools.md) for the tool reference.

## Version, license, and quick start

Run `help` in the CLI (`./run_cli.sh`) for a Quick Start covering the
typical configuration workflow, and `show version` (EXEC mode only) for
the exact version, release date, source revision, and license currently
running — see [docs/cli_reference.md](docs/cli_reference.md) for the full
`help`/`show version` reference (`?` remains the separate, IOS XR-style
context-sensitive syntax help). Inside a configuration mode, `show
running-config`/`show configuration` instead show the committed/candidate
state of whatever you are currently editing there — see
[docs/cli_reference.md](docs/cli_reference.md#show-running-config-vs-show-configuration).
`help claude` explains how Claude Code uses Network Lab MCP.

Network Lab MCP is licensed under the **GNU General Public License v3.0**
(see [LICENSE](LICENSE)). Version (`0.1.0`), author, and license metadata
are declared once in `pyproject.toml` and read at runtime via
`importlib.metadata` — `show version`'s output and this README are the
same source, never two independently maintained copies.
