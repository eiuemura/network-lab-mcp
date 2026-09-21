# Architecture

Network Lab MCP provides a lightweight "AI Network Engineer Layer" designed to
maximize the reasoning capabilities of AI in network lab environments.

Network Lab MCP is **not** a network engineering reasoning engine. It is a
thin layer that gives Claude Code (or any other MCP client) two things:

1. **Lab knowledge** — where the work happens, how to behave, what to
   accomplish, and what reusable knowledge already exists.
2. **Terminal access** — a real, general-purpose way to interact with lab
   devices, close to how a human operator would use a terminal, without
   ever handing Claude the private connection details.

All actual network engineering judgment — what command to run next, how to
interpret output, when a task is done — is left to Claude Code.

## Configuration model

Five kinds of lab data are deliberately kept separate (see README.md's
[Configuration model](../README.md#configuration-model) for the reader-facing
summary):

```
Human
  |
  +--> running-config
  |      access-info / topology / scenario / references selected for MCP
  |      (lab/settings.yaml -- a SELECTION, not a definition)
  |
  +--> access-info
  |      private device access (address/transport/port/username/password)
  |      (lab/access-info/*.yaml -- never exposed to Claude)
  |
  +--> topology
  |      safe logical model (devices, device type, links)
  |      (lab/topologies/*.yaml -- exposed to Claude)
  |
  +--> scenario
  |      what Claude should do (lab/scenarios/*.yaml)
  |
  +--> reference
  |      reusable knowledge (lab/references/*.yaml)
  |
  v
Network Lab MCP
  |
  +--> safe topology
  +--> scenario
  +--> references
  |
  v
Claude Code
```

`get_active_topology()` answers "where should I work?" `get_execution_instructions()`
answers "what rules must I follow, what must I accomplish, and what reusable
knowledge is available?" combining principles, the active scenario, and active
references into one structured result. Neither tool tells Claude Code *how*
to solve the problem, and neither ever returns access-info.

The Workspace is deliberately **not** managed by the MCP server. Claude Code
uses its own current working directory (the task workspace it was started
from) to store evidence, configurations, and reports, organized however the
task requires. Network Lab MCP has no `runtime/` directory and keeps no
records of what Claude Code produces.

## Request path

```
Claude Code
    |
Network Lab MCP
    |  get_active_topology()
    |  get_execution_instructions()
    |  terminal_open() / terminal_send() / terminal_read()
    |  terminal_list() / terminal_close()
    v
dedicated tmux environment (socket: network-lab-mcp)
    v
ssh / telnet
    v
Lab Devices
```

The MCP server itself is a thin translation layer: lab tools read YAML from
disk and return it as structured data; terminal tools translate a logical
device name into tmux commands, privately resolving access-info along the
way (see "Device access resolution" below). It holds essentially no state of
its own — tmux is the single source of truth for terminal session lifetime,
and lab YAML on disk is the single source of truth for running-config/
topology/access-info/scenario/reference content.

## Device access resolution

`terminal_open(device)` receives only a logical device name from Claude —
never an address, username, or password, since topology no longer carries
private access fields at all.

```
Claude / terminal_open("R1")
  |
  v
MCP reads committed running-config, resolves active topology
  |
  v
MCP verifies R1 exists in active topology
  |
  v
MCP private access resolver
  |
  +--> resolve running-config's selected access-info (active_access_info)
  |
  +--> none selected           -> fail closed
  +--> selected file missing   -> fail closed
  |
  +--> load ONLY that access-info definition, look up R1
  |
  +--> not present in it       -> fail closed  (no other file is searched)
  |
  +--> compare topology/access-info device type
  |       mismatch -> fail closed
  |
  +--> device has a jump_host reference?
  |       yes -> resolve it within the same access-info definition
  |              -> attach as jump_host_config (fails closed if the
  |                 referenced jump host is somehow absent)
  |       no  -> nothing to attach
  |
  v
tmux / native OpenSSH (direct, or -J ProxyJump if jump_host_config is set) / telnet
  |
  v
Device (optionally via one jump host)
```

This is implemented by `lab.get_device()`: it reads
`get_active_access_info_name(settings)` (running-config's
`active_access_info` — a missing field means "none selected", not an
error), loads *only* that one access-info definition, looks up the device
in it, and (if the device references a `jump_host`) resolves that jump
host within the same definition. No credential value ever appears in a
"no access-info selected", "does not exist", "not present", "type
mismatch", or "unknown jump host" error.

### Access-info lookup is no longer global

An earlier version of this resolver searched every committed
`lab/access-info/*.yaml` file for a matching device ID and failed closed
on ambiguity if more than one file contained it — a temporary, unscoped
lookup used before running-config could explicitly select one access-info
definition. That global search is now removed entirely (not bypassed): only
the definition named by `active_access_info` is ever read. The same device
ID may safely appear in other, unselected access-info files; selecting a
different access-info in running-config (see `cli/config.py`'s
`select_access_info()`/`clear_access_info_selection()`) is what changes
which one resolves a given device.

### Single-hop OpenSSH ProxyJump

A device's optional `jump_host` field names one jump host, within the same
access-info definition, that `terminal.py` connects through using native
OpenSSH's own `-J` option (`ssh -J jump-user@jump-host:jump-port -p
port user@target`) instead of a direct connection —
`terminal._build_transport_command()` builds this argv (list-based, never
`shell=True`, never a password) purely from the resolved device/jump-host
dicts, with no shell-hop automation: OpenSSH itself tunnels the second SSH
connection through the first, and the tmux pane still only ever shows the
one resulting interactive session that `terminal_read()`/`terminal_send()`
already know how to drive. `lab.validate_access_info_data()` (via
`validate_jump_hosts()`/`validate_device_jump_host_references()`) is the
single validation primitive enforcing "jump host type is always `host`",
"jump host transport is always `ssh`", "a device using `jump_host` must
itself use `transport: ssh`", and "the referenced jump host exists" —
applied identically whether the data came from a CLI commit or a manually
edited file. Nesting one jump host behind another is not represented in
the schema at all, so multi-hop chains cannot occur even by mistake.

## Installation model and lab root ownership

Step 1 supports exactly one installation model: a local repository checkout
installed with `pip install -e .`. The repository checkout **owns** the
`lab/` directory. `network_lab_mcp.lab.find_lab_root()` resolves this
directory relative to the installed package's own source location (its
`__file__`), never relative to the current working directory of the process
that launched the MCP server — Claude Code is normally started from an
unrelated task workspace, so depending on its working directory would be
incorrect.

Non-editable or wheel installation (`pip install .`, a built wheel, or a
package-index install) is **not a supported configuration in Step 1**: such
an install has no `lab/` directory to find, since lab data is repository-local
operational data rather than a packaged resource. Supporting that would
require packaging work (e.g. `importlib.resources`, an external writable
config directory, or an environment-variable-based lab-root override) that is
explicitly deferred to a later step.

## Lab YAML reload policy

None of `get_active_topology()`, `get_execution_instructions()`, or
`terminal_open()` cache lab YAML at server startup. Each reads
`lab/settings.yaml` and whatever it references (including, for
`terminal_open()`, every `lab/access-info/*.yaml` file) fresh from disk on
every call. This means editing lab YAML, or committing a change from the
Step 2 CLI, takes effect on the very next tool call, without restarting the
MCP server.

## Terminal architecture

Terminal sessions are managed with tmux, using a dedicated named socket
(`tmux -L network-lab-mcp ...`) so that Network Lab MCP never interferes with
a user's own interactive tmux sessions. tmux itself — not any structure
inside the MCP server — is the source of truth for which sessions exist and
whether they are still running. The MCP server does not keep a session
database; a restarted server rediscovers existing sessions by asking tmux.

SSH and Telnet are launched as the operating system's own `ssh`/`telnet`
binaries, inside a managed tmux pane. Network Lab MCP does not use a Python
SSH library and does not use `pexpect` as its core mechanism, and it does not
attempt to parse device prompts (IOS XR or otherwise). Instead, Claude Code
reads the pane with `terminal_read()`, decides what a prompt means, and
responds with `terminal_send()` — the same loop a human operator would use.

### Structurally separate session namespaces

Two structurally distinct namespaces exist, chosen by the type of caller
(production tool vs. internal validation), never by pattern-matching on a
device's name:

```
Production:  network-lab-device-<device-id>
Validation:  network-lab-validation-<validation-id>
```

`derive_production_session_name()` and `derive_validation_session_name()` are
the only two functions that produce these names, and each only ever produces
a name in its own namespace. Because the two prefixes are fixed and distinct
strings, a session name can belong to at most one namespace — there is no
ambiguity to resolve at runtime. Concretely, a topology device literally
named `validation-router` maps to the production session
`network-lab-device-validation-router`; it is not, and cannot become, a
validation-namespace session such as `network-lab-validation-terminal-io`.

The public tools (`terminal_open`, `terminal_send`, `terminal_read`,
`terminal_list`, `terminal_close`) only ever derive and act on production
session names. A small set of internal validation helpers
(`open_validation_session`, `send_to_validation`, `read_validation`,
`list_validation_sessions`, `close_validation_session` in `terminal.py`) only
ever derive and act on validation session names. Both sets of callers share
the same underlying session-management primitives (session creation/reuse,
send text/keys, capture pane, list, close) — local validation exercises the
real production code path, just against a safe local command (e.g. `cat`)
instead of `ssh`/`telnet`.

## Step 2 / 2.5: the human configuration/control plane

The **Human Configuration / Control Interface** is an IOS XR-compatible CLI
(`./run_cli.sh`, `network_lab_mcp.cli`) that a human operator uses to create
and edit lab definitions and the running-config selection. It is a separate
process from the MCP server and never speaks the MCP stdio protocol.

```
Human Operator
    |
IOS XR-compatible CLI (./run_cli.sh)
    |
Candidate configuration (memory-only):
  - running-config candidate (settings_candidate)
  - at most one open definition candidate
    (topology | access-info | scenario | reference)
    |
commit
    |
Committed lab YAML:
  lab/settings.yaml, lab/topologies/*.yaml,
  lab/access-info/*.yaml, lab/scenarios/*.yaml,
  lab/references/*.yaml
    |
Network Lab MCP  ->  Claude Code
```

The CLI is a configuration/control plane, not a network-engineering
reasoning engine: it creates/edits *definitions* and the *running-config
selection*, never operates on network devices itself, and never decides
what Claude Code should do with a topology once committed.

### Two independent candidate scopes

Unlike Step 2's original design (where global `topology`/`scenario`/
`reference <name>` directly mutated the active selection), the candidate
model now has two scopes that are dirty, switched, and cleared
independently:

- **Running-config candidate** (`settings_candidate`): a snapshot of
  `lab/settings.yaml`, mutated only from `running` mode
  (`topology`/`scenario`/`reference`/`no reference`). A settings-only change
  never blocks opening or switching a definition, and vice versa.
- **Definition candidate** (`definition_kind` / `definition_name` /
  `definition_original` / `definition_candidate`): at most one of
  `topology`, `access_info`, `scenario`, or `reference` at a time. Opening a
  *different* definition while the current one is dirty is blocked
  (`can_switch_definition()`), the same caution the original topology-switch
  guard applied, generalized to all four kinds.

`commit()` validates both scopes, writes any dirty definition file(s)
first, then `lab/settings.yaml` last — a running-config selection may name a
definition that was only just created or edited in the very same commit,
and that definition must already exist on disk by the time the selection
referencing it is written.

### Command grammar as the single source of truth

`cli/grammar.py` builds one trie per CLI mode (EXEC, global, running-config,
topology + its nested device submode, access-info + its nested device
submode, scenario, reference) out of fixed keywords and argument slots.
Parsing, unique fixed-keyword abbreviation, Tab/Ctrl-I completion,
context-sensitive `?` help (including the `<cr>` marker and "select or
create" identifier help — existing candidates plus a creation hint),
dynamic candidate lookup, and invalid/incomplete/ambiguous command reporting
all walk that same trie — there is no separate parser table, completion
table, or help table to drift out of sync.

Dynamic completion (topology/scenario/reference/access-info/device names,
and the `ssh`/`telnet` transport and `device.type` enums) is supplied by
small, pure provider functions of the shape `provider(context, prefix) ->
candidates`. `context` is a read-only `grammar.CliContext` snapshot that
`cli/main.py` builds fresh from the live `cli/config.CliSession` before
every parse/completion/help call. `cli/grammar.py` never imports
`cli/config.py` and owns no mutable session state itself, so there is no
grammar/config circular dependency, and completion/help can never mutate
the candidate. It does import `network_lab_mcp.lab` for the `device.type`
enum SSOT (`DEVICE_TYPES` / `normalize_device_type()`), a one-directional
dependency that introduces no cycle.

### Fixed-keyword vs. object-identifier case semantics

Fixed CLI keywords (`configure`, `topology`, `transport`, ...) are matched
case-insensitively, mirroring IOS XR. Object identifiers — topology,
scenario, reference, access-info, and device names — are matched
case-sensitively and are never abbreviated or case-folded, because they are
user-provided/stored names, not grammar keywords. Dynamic completion for
identifiers preserves and matches on exact stored case (`R1`/`R2` completing
`device R`, but not `device r`).

### Case-only topology-name collision safeguard

`topology <name>` (in global configuration mode) can both select an
existing topology definition and implicitly create a new one, so a
differently-cased near-duplicate (`SRv6_Lab` vs. an existing `srv6_lab`) is
a real hazard: silently opening the existing one would ignore the
operator's exact input, and silently creating a new one would produce a
confusing, hard-to-notice second topology. `cli/config.find_case_only_collision()`
is a narrow check — case-only equality, nothing fuzzier — that, when
triggered, requires explicit `yes`/`no` confirmation before a distinct
topology candidate is created. Declining is a true no-op; confirming
preserves the exact entered case. This is a CLI-side creation-safety check,
not a replacement for the Step 1 topology validator below.

### Step 1 validator reuse

`cli/config.py` does not maintain its own topology/access-info/device-type
validation rules. `network_lab_mcp.lab.validate_topology_data()` (built on
`validate_topology_device_names()`, the topology-no-access-fields check, and
the shared `device.type` enum) and `lab.validate_access_info_data()` are the
single validation primitives used by both the MCP/CLI load paths and the
CLI commit path alike.

### Minimal, targeted persistence

`commit()` validates the whole candidate first (zero disk writes on
failure), then writes only the YAML files whose *scope* was actually dirty,
definition file(s) before `lab/settings.yaml`: an unchanged definition that
was merely opened is never rewritten, a running-config-only change never
touches definition YAML, and a fully clean commit writes nothing. Writes are
atomic (write to a sibling `.tmp` file, flush, `os.replace()`).

`commit()` itself never changes `cli/config.CliSession.mode` — `cli/main.py`
never sets it after a `_do_commit()` call, success or no-op. Navigating
between modes is `root` (jump straight to global configuration mode from
any nested submode, preserving candidate state, driven by
`CliSession.go_to_global()`), `exit` (exactly one level up, via the
`_EXIT_PARENT_MODE` table below), and `end` (a guarded jump to EXEC,
`overall_dirty`-gated, unchanged from earlier Step 2 behavior) — none of
which ever commits or clears. `cli/config._EXIT_PARENT_MODE` is the single
source of truth for "exit"'s per-mode destination; `cli/main.py`'s generic
`h_exit()` handler reads it instead of hard-coding one function per mode.

### External YAML editor (topology / scenario / reference)

`cli/editor.py` resolves an external editor ($VISUAL, then $EDITOR, then a
`vim` fallback with just enough options to make an unfamiliar YAML file
legible — never added when the operator chose their own editor), opens the
current definition candidate in a secure, unique `.yaml` temporary file
(never the committed file itself), and on a clean editor exit parses and
minimally validates the result (valid YAML, root is a mapping) before
installing it as the new candidate through the same
`replace_definition_candidate()` -> per-kind SSOT validator path `commit()`
uses. A non-zero editor exit, invalid YAML, or a non-mapping root leaves the
candidate untouched and reports a safe error (no raw editor content, no
credentials). The temporary file is always removed, on every path including
exceptions. access-info does not offer `edit` in this phase — its private,
private fields stay on the structured CLI path (see README.md's
"Password display policy" for how those fields are rendered).

### Committed-only MCP boundary

The MCP server never reads candidate configuration. `get_active_topology()`
and `get_execution_instructions()` only ever read `lab/settings.yaml` and
whatever it currently references on disk — exactly the same files a
successful `commit()` writes to. `terminal_open()` additionally reads
`lab/access-info/*.yaml`, but only ever the committed files, never a
candidate. There is no shared in-memory state, cache, temp file, or extra
MCP tool bridging the CLI process and the MCP server process; the boundary
is the committed YAML on disk, and Step 1's existing reload-on-every-call
policy means a successful commit is visible to Claude Code on the very next
tool call, with no MCP server restart.

## Not implemented yet

To keep the MCP layer thin and the scope tight, this repository still
deliberately excludes:

- Topology discovery (CDP/LLDP) and `discover topology` — this is Step 3.
- An HTTP MCP server, containerization, or an MCP-owned runtime/session
  database.
- A public local-shell transport (local processes are used only inside the
  internal validation helpers described above).
- External-editor support for access-info (structured CLI editing only in
  this phase).
- Multi-hop SSH jump chains: a jump host cannot itself reference another
  jump host; single-hop OpenSSH ProxyJump only.
- A shell-hop automation/state machine for ProxyJump: OpenSSH's own `-J`
  option is used directly, so there is nothing to automate.
- A generic cross-file consistency framework: the topology/access-info
  `device.type` check is a narrow, single-purpose comparison at resolution
  time, not a general validation framework.
- A persistent candidate journal or crash-recovery database: candidate
  configuration is memory-only and intentionally does not survive an abrupt
  CLI process termination (SIGKILL, crash, terminal destruction). Normal
  exit paths (`root`, `exit`, `end`, `clear`, Ctrl-D) never lose a candidate
  unexpectedly; that guarantee does not extend to a killed process.

These are candidates for later steps, not for this repository's current
scope.
