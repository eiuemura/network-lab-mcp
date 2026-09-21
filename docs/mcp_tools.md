# MCP Tools

For a shorter, CLI-native summary of this same integration (from the
operator's side), run `help claude` in `./run_cli.sh`.

Network Lab MCP exposes exactly seven tools over stdio. All lab-data tools
reload their inputs from disk on every call (see
[architecture.md](architecture.md#lab-yaml-reload-policy)). Tool results are
returned as structured JSON.

**Committed-state boundary (Step 2 / 2.5)**: these tools only ever read
*committed* `lab/settings.yaml`, `lab/topologies/*.yaml`, and (for
`terminal_open()` only) `lab/access-info/*.yaml` — the same files the Step 2
human CLI (`./run_cli.sh`) writes on a successful `commit`. Uncommitted
candidate configuration in a CLI session is never visible here; a
successful commit becomes visible on the very next call to any of these
tools, with no MCP server restart required. See
[architecture.md](architecture.md#committed-only-mcp-boundary) and
[cli_reference.md](cli_reference.md).

**Private access-info boundary**: `lab/access-info/*.yaml` (device
address/transport/port/username/password) is never returned by any tool
below. `get_active_topology()` returns only the safe logical topology;
`terminal_open()` resolves access-info internally to open a session, but
returns only the device name, session name, reuse flag, and transport —
never the address, username, or password.

## get_active_topology()

**Purpose**: answer "where should I work?" — return the currently active lab
topology.

**Arguments**: none.

**Return value**:

```json
{
  "active_topology": "sample",
  "topology": {
    "name": "sample",
    "description": "...",
    "devices": { "R1": { "type": "iosxr" } },
    "links": []
  }
}
```

Note that a device entry here never includes `address`, `transport`,
`port`, `username`, or `password` — topology only ever holds safe logical
data (`type` plus whatever future safe fields it grows). Private connection
data lives in a separate access-info definition that this tool never reads
or returns; see `terminal_open()` below for how that gets resolved when a
session is actually opened.

**Usage example**: call this first, before touching any device, to learn
which devices and links exist in the active topology.

**Important behavior**: `lab/settings.yaml` and the active topology YAML are
both re-read from disk on every call. If you edit `lab/settings.yaml` (or
commit a running-config change from the CLI) to point at a different
topology, the next call reflects that immediately.

**Error behavior**: raises a tool error (visible to the caller, not a crash)
when `lab/settings.yaml` is missing, the active topology name is invalid, the
referenced topology file does not exist or is not valid YAML, or the
topology's device names cannot be safely mapped to terminal sessions.

## get_execution_instructions()

**Purpose**: answer "what rules must I follow, what must I accomplish, and
what reusable knowledge is available?" — return principles, the active
scenario, and the active references together.

**Arguments**: none.

**Return value**:

```json
{
  "principles": { "workspace_principles": [...], "general_operating_principles": [...], "prohibited_actions": [...] },
  "scenario": { "name": "sample", "content": { "...": "..." } },
  "references": [ { "name": "sample", "content": { "...": "..." } } ]
}
```

**Usage example**: call this alongside `get_active_topology()` at the start
of a task to learn how to behave and what the task requires.

**Important behavior**: `lab/settings.yaml`, `lab/principles.yaml`, the
active scenario file, and every active reference file are all re-read from
disk on every call.

**Error behavior**: raises a tool error when `lab/settings.yaml` is missing,
the active scenario or any active reference name is invalid, or the
corresponding file does not exist or is not valid YAML.

## terminal_open()

**Purpose**: open a terminal session for a device in the active topology,
launching `ssh` or `telnet` inside the dedicated tmux environment. Reuses an
existing session for that device instead of creating a duplicate.

**Arguments**:

- `device` (string, required): the device name as it appears in the active
  topology's `devices` mapping.

**Return value**:

```json
{
  "device": "R1",
  "session_name": "network-lab-device-R1",
  "reused": false,
  "transport": "ssh"
}
```

**Usage example**: `terminal_open(device="R1")`, then use `terminal_read()`
to see the login prompt.

**Important behavior**: the active topology is reloaded from disk before
opening the session, so a device added to `lab/topologies/<active>.yaml` (or
a topology switch in `lab/settings.yaml`) is picked up without restarting the
server. `device` only needs to name a device that exists in the active
topology — Claude never supplies (or sees) an address, username, or
password. Internally, Network Lab MCP resolves the device's private
connection data from running-config's *selected* access-info definition
only (`active_access_info`) — see
[architecture.md](architecture.md#device-access-resolution); no other
committed access-info file is ever searched. If the resolved device
references a `jump_host` (within that same access-info definition),
Network Lab MCP connects via native OpenSSH ProxyJump instead of directly
— see [architecture.md](architecture.md#single-hop-openssh-proxyjump) —
transparently to Claude, which still only ever sees the one resulting
terminal session. This tool does not parse or automate login: password
prompts, host key confirmations, and any other interactive prompt are left
for the caller to observe via `terminal_read()` and respond to via
`terminal_send()`. This active-topology-membership restriction is
unchanged by Step 3: `discover topology`'s private bootstrap connectivity
(automated login, only for its own temporary sessions) is a completely
separate internal code path in a structurally distinct tmux namespace,
never reachable through this tool and never weakening it — see
[architecture.md](architecture.md#a-third-session-namespace-discovery-bootstrap).

**Error behavior**: raises a tool error (fail closed, never a silent guess)
when:

- the device is not present in the active topology;
- no access-info is selected in running-config (`% No access-info is
  selected in running-config.`);
- the selected access-info definition does not exist (`% Selected
  access-info '<name>' does not exist.`);
- the device is not present in the selected access-info definition (`%
  Device '<device>' is not present in access-info '<name>'.`) — there is
  no fallback search through any other access-info file;
- the topology and resolved access-info both specify `type` and, once
  normalized, they disagree (`% Device type mismatch for '<device>' between
  topology and access information.`);
- the device references a `jump_host` that does not exist in the same
  access-info definition;
- the device's transport is unsupported, the required `ssh`/`telnet` binary
  is unavailable, or the device name cannot be mapped to a valid session
  name.

No error message from this tool ever includes a credential value.

## terminal_send()

**Purpose**: send input to a device's open terminal session.

**Arguments**:

- `device` (string, required)
- `text` (string, optional): literal text to send (not interpreted as tmux
  key names).
- `keys` (list of strings, optional): special keys to send, e.g. `"C-c"`,
  `"Tab"`, `"Space"`, `"Up"`, `"Enter"`, `"M-x"`.
- `enter` (boolean, optional, default `false`): send Enter last.

**Return value**:

```json
{ "device": "R1", "session_name": "network-lab-device-R1" }
```

**Usage example**:

```
terminal_send(device="R1", text="show version", enter=true)
```

**Important behavior — deterministic execution order**. When more than one
of `text`, `keys`, and `enter=true` is supplied, they are always applied in
this fixed order, with no deduplication:

1. If `text` is given, it is sent literally first.
2. If `keys` are given, they are sent next, in the supplied order.
3. If `enter` is `true`, Enter is sent last.

Examples:

| Call | Effect |
|------|--------|
| `text="show version", enter=true` | send `"show version"`, then Enter |
| `keys=["C-c"]` | send only Ctrl-C |
| `text="something", keys=["Tab"], enter=true` | send `"something"`, then Tab, then Enter |
| `keys=["Enter"], enter=true` | Enter is sent **twice** — nothing is deduplicated |

`text` may contain credentials (e.g. a password prompt response). It is
never logged, never persisted, and never echoed back in the tool's response.

**Error behavior**: raises a tool error when the device's session does not
exist (call `terminal_open()` first) or an unsupported key name is given in
`keys`.

## terminal_read()

**Purpose**: capture recent terminal output for a device, so the caller can
see the current prompt, command output, a password/interactive prompt,
paging state, configuration-mode/administrative-mode transitions, or
unexpected errors.

**Arguments**:

- `device` (string, required)
- `lines` (integer, optional, default `100`): how many recent lines of
  content to return.

**Return value**:

```json
{ "device": "R1", "session_name": "network-lab-device-R1", "content": "..." }
```

**Usage example**: `terminal_read(device="R1", lines=200)` after sending a
command that produces a lot of output.

**Important behavior**: this tool does not attempt to parse or classify
device prompts (IOS XR or otherwise) — it hands back raw recent pane
content and leaves interpretation to the caller. The underlying tmux session
retains a much larger scrollback buffer than the default `lines`, so a
larger `lines` value can be requested when needed without losing history.

**Error behavior**: raises a tool error when the device's session does not
exist.

## terminal_list()

**Purpose**: list managed production terminal sessions for active-topology
devices.

**Arguments**: none.

**Return value**:

```json
{
  "sessions": [
    { "device": "R1", "session_name": "network-lab-device-R1", "state": "running" }
  ]
}
```

**Usage example**: call this to see which devices already have an open
session before deciding whether to call `terminal_open()` again.

**Important behavior**: only sessions in the `network-lab-device-*`
namespace are ever returned. Local validation sessions
(`network-lab-validation-*`) are never exposed as topology devices by this
tool, regardless of what a device happens to be named — classification is by
structural namespace prefix, not by searching for the substring
`"validation"`. A legitimate device named `validation-router` (session
`network-lab-device-validation-router`) is listed normally.

**Error behavior**: does not raise on an empty session list; returns
`{"sessions": []}`.

## terminal_close()

**Purpose**: close a device's managed production terminal session.

**Arguments**:

- `device` (string, required)

**Return value**:

```json
{ "device": "R1", "session_name": "network-lab-device-R1", "closed": true }
```

`closed` is `false` (not an error) if there was no session to close.

**Usage example**: `terminal_close(device="R1")` once a task involving that
device is finished.

**Important behavior**: this tool always derives a production session name
(`network-lab-device-<device>`) and can never target a validation-only
session, even if a caller-supplied name happens to look similar to a
validation identifier.

**Error behavior**: raises a tool error only when the device name itself
cannot be mapped to a valid session name; a missing session is reported as
`closed: false`, not as an error.
