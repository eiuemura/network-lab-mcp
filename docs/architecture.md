# Architecture

Network Lab MCP provides a lightweight "AI Network Engineer Layer" designed to
maximize the reasoning capabilities of AI in network lab environments.

Network Lab MCP is **not** a network engineering reasoning engine. It is a
thin layer that gives Claude Code (or any other MCP client) two things:

1. **Lab knowledge** — where the work happens, how to behave, what to
   accomplish, and what reusable knowledge already exists.
2. **Terminal access** — a real, general-purpose way to interact with lab
   devices, close to how a human operator would use a terminal.

All actual network engineering judgment — what command to run next, how to
interpret output, when a task is done — is left to Claude Code.

## Responsibility model

```
Topology
    -> WHERE / WHAT EXISTS
    -> Where the work is performed and what devices and links exist

Principles
    -> HOW TO BEHAVE
    -> Common operating rules that apply to every scenario

Scenario
    -> WHAT TO DO
    -> What must be accomplished for the current task

References
    -> REUSABLE KNOWLEDGE
    -> Reusable validated guidance, operational knowledge,
       known values, and lab-specific know-how

Terminal
    -> ACTION INTERFACE
    -> How Claude interacts with lab devices

Claude Code
    -> REASONING
    -> Determines how to accomplish the task

Workspace
    -> TASK ARTIFACT AREA
    -> Where Claude stores evidence, analysis, configurations,
       validation results, designs, and reports
```

`get_active_topology()` answers "where should I work?" `get_execution_instructions()`
answers "what rules must I follow, what must I accomplish, and what reusable
knowledge is available?" combining principles, the active scenario, and active
references into one structured result. Neither tool tells Claude Code *how*
to solve the problem — that is Claude Code's own job.

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
device name into tmux commands. It holds essentially no state of its own —
tmux is the single source of truth for terminal session lifetime, and lab
YAML on disk is the single source of truth for topology/principles/scenario/
reference content.

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
`lab/settings.yaml` and whatever it references fresh from disk on every call.
This means editing `lab/settings.yaml` to switch the active topology,
scenario, or references takes effect on the very next tool call, without
restarting the MCP server.

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

## Step 2: the human configuration/control plane

Step 2 adds a fourth role alongside Topology/Principles/Scenario/References/
Terminal/Claude Code/Workspace: the **Human Configuration / Control
Interface**, an IOS XR-compatible CLI (`./run_cli.sh`,
`network_lab_mcp.cli`) that a human operator uses to edit lab configuration.
It is a separate process from the MCP server and never speaks the MCP stdio
protocol.

```
Human Operator
    |
IOS XR-compatible CLI (./run_cli.sh)
    |
Candidate configuration (memory-only)
    |
commit
    |
Committed lab YAML (lab/settings.yaml, lab/topologies/*.yaml)
    |
Network Lab MCP  ->  Claude Code
```

The CLI is a configuration/control plane, not a network-engineering
reasoning engine: it edits *lab configuration* (topology/device definitions,
active scenario/reference selection), never operates on network devices
itself, and never decides what Claude Code should do with a topology once
committed.

### Candidate configuration

Entering `configure` takes a private, in-memory snapshot of committed
settings (`lab/settings.yaml`) into a **settings candidate**; the full active
topology is deliberately *not* loaded at this point. Selecting a topology
(`topology <name>`) then loads (or creates) a separate **topology
candidate**. Both candidates are pure Python data (the same shapes as the
settings/topology YAML) held only in the CLI process's memory; nothing is
written to disk until `commit` succeeds.

### Scoped dirty state

Rather than one global dirty flag, three independent scopes are tracked:

- `settings_dirty` — the settings candidate differs from the committed
  settings snapshot (e.g. a different active scenario/reference selection).
- `topology_dirty` — an *existing* topology candidate differs from the
  topology as loaded from disk.
- `new_topology_dirty` — the selected topology does not exist on disk yet
  (a `topology <name>` that created a new topology candidate).

`overall_dirty` is the logical OR of the three. This separation is what lets
a settings-only change (e.g. `scenario troubleshoot`) move freely between
topologies, while an in-progress topology edit blocks switching away from it
until `commit` or `abort`.

### Command grammar as the single source of truth

`cli/grammar.py` builds one trie per CLI mode (EXEC, global configuration,
topology configuration, device configuration) out of fixed keywords and
argument slots. Parsing, unique fixed-keyword abbreviation, Tab/Ctrl-I
completion, context-sensitive `?` help (including the `<cr>` marker),
dynamic candidate lookup, and invalid/incomplete/ambiguous command reporting
all walk that same trie — there is no separate parser table, completion
table, or help table to drift out of sync.

Dynamic completion (topology/scenario/reference/device names, and the
`ssh`/`telnet` transport enum) is supplied by small, pure provider functions
of the shape `provider(context, prefix) -> candidates`. `context` is a
read-only `grammar.CliContext` snapshot that `cli/main.py` builds fresh from
the live `cli/config.CliSession` before every parse/completion/help call.
`cli/grammar.py` never imports `cli/config.py` and owns no mutable session
state itself, so there is no grammar/config circular dependency, and
completion/help can never mutate the candidate.

### Fixed-keyword vs. object-identifier case semantics

Fixed CLI keywords (`configure`, `topology`, `transport`, ...) are matched
case-insensitively, mirroring IOS XR. Object identifiers — topology,
scenario, reference, and device names — are matched case-sensitively and
are never abbreviated or case-folded, because they are user-provided/stored
names, not grammar keywords. Dynamic completion for identifiers preserves
and matches on exact stored case (`R1`/`R2` completing `device R`, but not
`device r`).

### Case-only topology-name collision safeguard

`topology <name>` can both select an existing topology and implicitly create
a new one, so a differently-cased near-duplicate (`SRv6_Lab` vs. an existing
`srv6_lab`) is a real hazard: silently opening the existing one would ignore
the operator's exact input, and silently creating a new one would produce a
confusing, hard-to-notice second topology. `cli/config.find_case_only_collision()`
is a narrow check — case-only equality, nothing fuzzier — that, when
triggered, requires explicit `yes`/`no` confirmation before a distinct
topology candidate is created. Declining is a true no-op; confirming
preserves the exact entered case. This is a CLI-side creation-safety check,
not a replacement for the Step 1 topology validator below.

### Step 1 validator reuse

`cli/config.py` does not maintain its own topology/device validation rules.
`network_lab_mcp.lab.validate_topology_data()` — a thin wrapper around
Step 1's `validate_topology_device_names()` — is the single validation
primitive used by `lab.load_topology()` (the MCP load path), `lab.write_topology()`
(the CLI commit path), and the test suite alike.

### Minimal, targeted persistence

`commit()` validates the whole candidate first (zero disk writes on
failure), then writes only the YAML files whose *scope* was actually dirty:
an unchanged topology that was merely selected is never rewritten, a
settings-only change never touches topology YAML, and a fully clean commit
writes nothing. Writes are atomic (write to a sibling `.tmp` file, flush,
`os.replace()`).

### Committed-only MCP boundary

The MCP server never reads candidate configuration. `get_active_topology()`
and `get_execution_instructions()` only ever read `lab/settings.yaml` and
whatever it currently references on disk — exactly the same files a
successful `commit()` writes to. There is no shared in-memory state, cache,
temp file, or extra MCP tool bridging the CLI process and the MCP server
process; the boundary is the committed YAML on disk, and Step 1's existing
reload-on-every-call policy means a successful commit is visible to Claude
Code on the very next tool call, with no MCP server restart.

## Not implemented in Step 1 or Step 2

To keep the MCP layer thin and the scope tight, this repository still
deliberately excludes:

- Topology discovery (CDP/LLDP), and topology write/delete (`no topology
  <name>`, `show topology`, `discover topology`) — this is Step 3.
- An HTTP MCP server, containerization, or an MCP-owned runtime/session
  database.
- A public local-shell transport (local processes are used only inside the
  internal validation helpers described above).
- Scenario, reference, and principles *content* editing from the CLI
  (selection only — see [scenario_format.md](scenario_format.md)).
- A persistent candidate journal or crash-recovery database: candidate
  configuration is memory-only and intentionally does not survive an abrupt
  CLI process termination (SIGKILL, crash, terminal destruction). Normal
  exit paths (`exit`, `end`, `abort`, Ctrl-D) never lose a candidate
  unexpectedly; that guarantee does not extend to a killed process.

These are candidates for later steps, not for this repository's current
scope.
