# Network Lab MCP

**An AI Network Engineer Workspace for real network labs.**

## Why I built it

I work as a network engineer, and I often find myself wanting to validate
something in a lab but simply not having enough time to do it. Customer
work, troubleshooting, meetings, documentation, and many other tasks
usually come first. The lab work is important, but it is also
time-consuming.

That made me wonder: what if I could ask AI to do the lab investigation for
me? Not just run a command, but understand the topology, connect to the
right devices, inspect the current state, compare results, investigate
problems, and report back with evidence.

At the same time, I did not want the AI to become a black box. As a network
engineer, I still want to see what it is doing and make the final
engineering decisions.

Network Lab MCP is the result: a thin foundation that lets Claude Code (or
any other MCP client) understand a lab's topology, follow common operating
principles, understand the current task, draw on reusable reference
knowledge, and reach lab devices through a real terminal — while every
terminal session stays human-observable, and every actual network
engineering decision stays with the engineer.

## What it does

Network Lab MCP is **not** a fixed test-automation tool, and it is **not** a
network-engineering reasoning engine. It gives an MCP client two things:

1. **Lab knowledge** — where the work happens, how to behave, what to
   accomplish, and what reusable knowledge already exists.
2. **Terminal access** — a real, general-purpose way to interact with lab
   devices, close to how a human operator would use a terminal, without
   ever handing the AI the private connection details.

All actual network engineering judgment — what command to run next, how to
interpret output, when a task is done — is left to Claude Code.

It also includes an independent, IOS XR-compatible human-facing CLI
(`./run_cli.sh`) for creating and editing lab definitions (topology,
private access information, scenario, reference) through a candidate/commit
model, and for observing terminal activity live while the AI works.

## How it works

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

Five kinds of lab data are deliberately kept separate:

| Concept | Role | Exposed to Claude? |
|---------|------|---------------------|
| **running-config** | *WHICH* topology/scenario/references MCP currently uses — a **selection**, stored in `lab/settings.yaml` | Indirectly (drives which topology/scenario/references are read) |
| **access-info** | *HOW TO ACCESS* devices — private connection data (address/transport/port/username/password), `lab/access-info/*.yaml` | **Never** |
| **topology** | *WHAT EXISTS / HOW IT IS CONNECTED* — safe logical devices, device type, links, `lab/topologies/*.yaml` | Yes, via `get_active_topology()` |
| **scenario** | *WHAT TO DO* for the current task, `lab/scenarios/*.yaml` | Yes, via `get_execution_instructions()` |
| **reference** | Reusable, validated knowledge, `lab/references/*.yaml` | Yes, via `get_execution_instructions()` |

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
`running-config` change does that.

Placed alongside Principles, Terminal, Claude Code, and Workspace, the full
responsibility model looks like this:

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

The Workspace is deliberately **not** managed by Network Lab MCP: Claude
Code uses its own current working directory (the task workspace it was
started from) to store evidence, configurations, and reports, organized
however the task requires. See [docs/architecture.md](docs/architecture.md)
for the full picture, including the device-access resolution flow.

## Key design principles

- **Exactly seven MCP tools, and nothing else.** `get_active_topology`,
  `get_execution_instructions`, `terminal_open`, `terminal_send`,
  `terminal_read`, `terminal_list`, `terminal_close`. There is no batch/
  parallel tool, no config-mutation tool, and no tool that returns
  access-info. See [docs/mcp_tools.md](docs/mcp_tools.md).
- **Real, human-observable terminals.** Terminal sessions run in a
  dedicated tmux environment, driven the same way a human operator would
  drive one: read the pane, decide what to send, send it. Nothing here
  parses device prompts or maintains a device-CLI state machine — Claude
  Code does that reasoning itself. A human can watch the exact same session
  live with `monitor terminal <device>`.
- **Private access-info, never exposed to Claude.** Device addresses,
  usernames, and passwords live in a separate `access-info` definition that
  no MCP tool ever returns. `terminal_open(device)` receives only a logical
  device name; Network Lab MCP resolves the private connection details
  itself.
- **Candidate/commit configuration, IOS XR-style.** The human CLI edits
  lab definitions and the running-config selection through a
  candidate → commit model — nothing is written to disk, and nothing is
  visible to Claude Code, until an explicit `commit` succeeds.
- **Discovery is candidate-first, never automatic.** `discover topology`
  populates a topology *candidate* for human review; it never auto-commits
  and never auto-selects the result as the active topology.
- **Fail closed, not silently guessed.** A missing access-info selection, an
  unresolvable device, a device-type mismatch between topology and
  access-info, an ambiguous Discovery neighbor — every one of these is a
  clear, sanitized error rather than a guess.

## Quick Start

```bash
cd ~/work/network-lab-mcp

python3 -m venv .venv
source .venv/bin/activate

pip install -e .
```

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

Set up your local running-config selection once:

```bash
cp lab/settings.example.yaml lab/settings.yaml
```

Then start Claude Code from whatever task workspace you like (it does not
need to be this repository):

```bash
cd ~/work/customer-lab-investigation
claude
```

Network Lab MCP does not depend on that working directory. One Network Lab
MCP checkout owns exactly one lab root, and that lab root can contain
multiple topologies, access-info definitions, scenarios, and references;
`lab/settings.yaml` selects which topology/scenario/references are
currently in effect.

> **Advanced / isolated-lab-only**: some workflows run Claude Code with
> `claude --permission-mode bypassPermissions` to avoid per-command approval
> prompts. This skips Claude Code's own safety confirmations entirely and is
> only appropriate in an isolated lab environment you fully control — you
> are assuming that risk yourself. It is not the normal or recommended way
> to run Claude Code against this project.

Run the human configuration CLI to review or edit lab definitions:

```bash
./run_cli.sh
network-lab#
```

Run `help` inside it for a Quick Start covering the typical configuration
workflow, and `help claude` for how Claude Code uses Network Lab MCP.

### Supported installation model

Network Lab MCP supports exactly one deployment model: a local repository
checkout, installed with `pip install -e .`. The repository checkout owns
the `lab/` directory, and the MCP server resolves it relative to its own
source location — never relative to the current working directory of
whatever process launched it. `pip install .`, installing from a built
wheel, or `pip install network-lab-mcp` from a package index are **not
supported** — `lab/` is repository-local operational data, not a Python
package resource, so a non-editable install has no lab directory to find.

## Example workflow

```
network-lab# configure
network-lab(config)# discover topology
Discovering topology from access-info 'sample'...
Discovery complete.

  Access-info:          sample
  IOS XR targets:       2
  IOS XE targets:       0
  IOS targets:          0
  Connected:            2
  LLDP observations:    4
  CDP observations:     0
  Managed links:        2
  Unresolved neighbors: 0
  L3 enrichment:        2/2 devices, 2 interfaces
  Topology candidate:   sample

network-lab(config-topology-sample)# show configuration
...
network-lab(config-topology-sample)# commit
Commit complete.
network-lab(config-topology-sample)# root
network-lab(config)# running-config
network-lab(config-running)# topology sample
network-lab(config-running)# commit
Commit complete.
```

`get_active_topology()` (and every other MCP tool) keeps returning the
previously active topology until that final explicit running-config
`topology`/`commit` step — Discovery's own commit only persists the
topology *definition*, never the active selection.

From there, a typical Claude Code session calls `get_active_topology()` and
`get_execution_instructions()` to learn where to work and what to
accomplish, then `terminal_open()`/`terminal_send()`/`terminal_read()` to
investigate the devices that matter for the task, reporting back with
evidence saved in its own task workspace.

## Human-observable terminals

Because every session lives in a dedicated tmux environment, an engineer
can watch exactly what the AI is doing, live, without interfering with it:

```
network-lab# monitor terminal R1
RP/0/RP0/CPU0:R1#show version
...
RP/0/RP0/CPU0:R1#

--------------------------------------------------------------------------------
Monitoring terminal R1 | Read-only | Source: managed | Status: active | q: quit
--------------------------------------------------------------------------------
```

`monitor terminal <device-id>` (EXEC-only, strictly read-only) streams the
currently preferred terminal session's activity into the local terminal
while keeping a live status bar at the bottom — everything it prints stays
in the terminal emulator's own scrollback, exactly like ordinary command
output. It prefers a normal managed session, falling back to an active
Discovery bootstrap session when no managed session exists yet, and several
independent monitors (of the same or different devices) can run at once
from separate `./run_cli.sh` windows. See
["`monitor terminal`"](docs/cli_reference.md#monitor-terminal) in the CLI
reference for the full behavior.

Historical evidence does not depend on the monitor being open: every device
session, managed or Discovery, is also captured to a persistent per-session
log file (`logs/terminal/<device-id>/*.log`), reviewable with `show logging`
— see [docs/cli_reference.md](docs/cli_reference.md#show-logging).

## Security model

### Credentials never reach the AI

Real access-info YAML stores device usernames and passwords directly (this
is a lab tool; it does not introduce a separate secret store). Credentials
are used only to drive interactive terminal login. Network Lab MCP:

- never returns access-info from any MCP tool: `get_active_topology()`
  returns only the safe topology, and `get_execution_instructions()` never
  includes it either;
- never logs passwords or `terminal_send()` input text, and never echoes
  that input text back in tool responses;
- never includes credentials in generated documentation or error messages;
- never persists credentials into any separate runtime database — tmux is
  the only session state, and there isn't a second one.

`terminal_open(device)` receives only a logical device name from Claude —
never an address, username, or password. Network Lab MCP resolves the
private connection details itself:

1. Read the committed running-config and resolve the active topology.
2. Verify the device exists in the active topology.
3. Resolve the running-config's *selected* access-info
   (`active_access_info`) — **fail closed** if none is selected, or if the
   selected definition does not exist on disk.
4. Load **only that one** access-info definition and look up the device in
   it — **fail closed** if it is absent. No other access-info file is ever
   searched.
5. If both the topology and the resolved access-info specify `type`,
   normalize both through the shared device-type enum and compare — **fail
   closed** on a mismatch.
6. If the device has an optional `jump_host` reference, resolve it within
   the same access-info definition and attach it for a single-hop OpenSSH
   ProxyJump connection (see ["Single-hop SSH jump hosts"](#single-hop-ssh-jump-hosts-proxyjump)
   below); otherwise connect directly.
7. Only then does the tmux/ssh/telnet path run. Once the session exists,
   `terminal_open()` also completes private target authentication if the
   target's own login prompt actually appears — see
   ["Private managed-terminal authentication"](#private-managed-terminal-authentication)
   below.

Selecting which access-info definition this resolution reads is done
through `running-config`'s `access-info <name>` / `no access-info` — the
same candidate/commit model as topology/scenario/reference selection, and
just as invisible to `terminal_open()` until committed.

### Private managed-terminal authentication

Network Lab MCP can complete target authentication for a managed terminal
over **either transport**, using the selected private access-info
definition — SSH password authentication, or classic-IOS-style Telnet
username/password login. Credentials remain inside Network Lab MCP and are
not exposed to the AI/MCP client:

```
Claude
   |
   | terminal_open(R1)
   v
Network Lab MCP
   |
   +--> committed active access-info
   |        |
   |        +--> private username/password
   |
   v
native SSH/Telnet in tmux
   |
   +--> verify target login prompt (SSH password prompt, or Telnet
   |    Username:/Password:)
   |
   +--> send credentials privately, via a stdin-based tmux buffer paste --
   |    never as a command-line argument to any process
   |
   v
authenticated terminal
```

Neither transport's credentials ever cross the MCP boundary, appear in any
process's command-line arguments, or appear in an exception message.
`terminal_open()` recognizes only OpenSSH's own client-side password prompt
for SSH (never a device-CLI-specific prompt, so this works for every device
type, not just IOS XR) and only answers it once that prompt can be
confidently attributed to the *target* device — a jump host's own password
prompt (single-hop ProxyJump) is never answered; that hop must still use
non-interactive key/agent authentication. Key/agent authentication that
succeeds without ever showing a password prompt is completely
unaffected — no password is sent. For Telnet, only a bounded classic-IOS-
style login sequence is automated (optional `Username:`, then `Password:`,
then the device's own exec prompt) — never a generic prompt-answering loop,
and never enable/TACACS/OTP/MFA automation. If authentication definitively
fails or is rejected (either transport), `terminal_open()` fails with a
sanitized error (never the credential itself) and, if it created a new
session for this attempt, closes it; a pre-existing session is never
destroyed just because a later open encounters an unusual state, and an
already-authenticated session stays fully idempotent (no send, no
disturbance). See
[docs/architecture.md](docs/architecture.md#managed-terminal-private-authentication)
for the full design.

Telnet itself remains unencrypted, transmitting the login and all session
content in the clear — Network Lab MCP only keeps the credential private
from the AI/MCP client, it does not (and cannot) make Telnet a secure
transport. Telnet remains appropriate only for isolated lab environments.

### Password display policy

Network Lab MCP is primarily a lab tool, so **explicit local CLI
configuration display** — `show running-config` / `show configuration` /
bare `show` for an access-info device or jump host — shows `password` in
**clear text**, not masked:

```
network-lab(config-access-device-R1)# show running-config
access-info sample
 device R1
  type iosxr
  address 192.0.2.11
  transport ssh
  port 22
  username example-user
  password example-password
 !
!
```

This is the **only** place a password is ever shown in clear text. Every
other boundary is unchanged and unweakened:

- MCP tool results (`get_active_topology()`, `get_execution_instructions()`,
  every `terminal_*()` return value) never include it.
- Logs, exceptions, and every `%`-prefixed error message never include it.
- `?` help and Tab completion never reveal or offer it as a candidate.
- The CLI's in-memory command history never retains a password-setting
  command, even abbreviated.

### Single-hop SSH jump hosts (ProxyJump)

access-info can declare reusable `jump_hosts`, each a generic endpoint
(`type: host` — never a network-device type) reached over SSH:

```yaml
name: sample

jump_hosts:
  jump1:
    type: host
    address: 192.0.2.10
    transport: ssh
    port: 22
    username: example-user
    password: example-password

devices:
  R1:
    type: iosxr
    address: 192.0.2.11
    transport: ssh
    port: 22
    username: example-user
    password: example-password
    jump_host: jump1
```

A device's optional `jump_host` field references one jump host by name
within the *same* access-info definition. `terminal_open()` then launches
native OpenSSH with `-J` (conceptually `ssh -J
example-user@192.0.2.10:22 -p 22 example-user@192.0.2.11`) instead of
connecting directly — no shell-hop automation, just OpenSSH's own ProxyJump
tunneling one SSH connection through another. The tmux pane still only ever
shows one interactive session to read/send against, exactly like a direct
connection.

Constraints (all enforced by `lab.validate_access_info_data()`, so a
manually edited, invalid committed file fails closed the same way a
rejected CLI commit would): a jump host's `type` must resolve to exactly
`host`; a jump host's `transport`, if set, must be `ssh`; a device's
`transport` must also be `ssh` when it references a `jump_host`; and
single-hop only — a jump host has no `jump_host` field of its own. See
[docs/cli_reference.md](docs/cli_reference.md) for the full command
reference (`jump-host <name>` under access-info definition mode).

### Dedicated tmux environment

All Network Lab MCP terminal sessions run on a dedicated tmux server,
separate from any tmux environment you use interactively, so
`terminal_list()`/`terminal_close()` are always safe from interfering with
unrelated sessions. Production topology-device sessions
(`network-lab-device-<device-id>`), Discovery's private bootstrap sessions
(`network-lab-discovery-<device-id>`), and local validation-only sessions
(`network-lab-validation-<validation-id>`) live in structurally distinct
namespaces, distinguished only by a fixed prefix — never by pattern-matching
on a device's name, so a topology device literally named `validation-router`
maps to the ordinary production session
`network-lab-device-validation-router`, not a validation session. Terminal
sessions live in tmux independent of the MCP server process: the server
keeps no session registry of its own, never destroys a session on exit, and
a restarted server rediscovers and reuses any existing session instead of
creating a duplicate.

### stdio / stdout rule

The MCP server communicates over stdio, and stdout is reserved for MCP
protocol traffic. It never uses `print()`, prints no startup banner, and
sends all diagnostic logging to stderr.

## Supported device types & Discovery scope

A device's optional `type` field, when present (in either topology or
access-info), must be one of:

- `iosxr` — Cisco IOS XR
- `iosxe` — Cisco IOS XE
- `ios` — classic Cisco IOS (its own explicit type, never a compatibility
  label under `iosxe` — a device that actually runs classic IOS, not IOS
  XE, should be typed `ios`)
- `nxos` — Cisco NX-OS
- `host` — generic host / endpoint (e.g. a jump host, or a traffic
  generator)

`lab.normalize_device_type()` is the single validation primitive for this
enum: an unambiguous abbreviation like `type nx` normalizes to `nxos`, and
exact `ios` always wins over abbreviation resolution (never rejected merely
because it is also a prefix of `iosxr`/`iosxe`).

### Topology discovery

`discover topology` (global configuration mode only) reads the committed
`active_access_info` and runs read-only neighbor discovery against its
`iosxr`, `iosxe`, and `ios` devices — `host` is skipped (not an error);
`nxos` is unsupported and skipped:

| Device type | Discovery protocols |
|---|---|
| `iosxr` | LLDP + CDP |
| `iosxe` | LLDP + CDP |
| `ios` (classic IOS) | CDP only |

It never installs/activates a package, enables LLDP/CDP, or changes router
configuration; it requires all selected targets to succeed for neighbor
discovery (any login/command/timeout failure fails the whole operation
before the prior candidate is touched), and it always ends in topology
configuration mode with the result applied as a **candidate** — exactly
like a manually typed `topology <name>`. It never commits and never changes
`active_topology` itself; committing the result, and separately selecting
it as the active topology, both remain explicit human steps. NX-OS
discovery, SNMP/NETCONF/RESTCONF, multi-hop jump chains, and a generic
discovery/plugin framework are all out of scope. See
[docs/architecture.md](docs/architecture.md#topology-discovery) for the
full pipeline, including multi-protocol link reconciliation and how an
unresolved neighbor's evidence stays reviewable through the observing
device's own terminal log.

### L3 topology enrichment

Alongside neighbor discovery, each interface with a directly observed IPv4
address may also be enriched with that address plus its VRF:

```yaml
devices:
  R1:
    type: iosxr
    interfaces:
      GigabitEthernet0/0/0/2:
        ipv4_address: 10.0.12.1
        vrf: default
```

This is deliberately narrow: no prefix length is ever inferred, no
subnet/link inference from addresses is ever performed, and no operational
state (up/down, holdtime, counters) is ever persisted into topology — ask
the device directly for that. `get_active_topology()` returns this data
with no schema change, since it already returns the whole validated
topology mapping verbatim.

### Real-lab acceptance tests are gated

Real-lab tests are never run by a plain `pytest`:

```
NETWORK_LAB_REAL_TESTS=1 pytest tests/test_real_lab_iosxr.py -v --tb=line
```

(`--tb=line`, and never `--showlocals`, so a real device's password never
ends up in a failure traceback.)

## Documentation

- [docs/architecture.md](docs/architecture.md) — the full system
  architecture: design goals, configuration model, MCP interface, device
  access, terminal architecture, topology discovery, the CLI control plane,
  security boundaries, and persistence/lifecycle.
- [docs/mcp_tools.md](docs/mcp_tools.md) — the current seven-tool MCP
  contract.
- [docs/cli_reference.md](docs/cli_reference.md) — the full human CLI
  command reference.
- [docs/scenario_format.md](docs/scenario_format.md) — the current
  scenario/reference format.

### Directory structure

```
network-lab-mcp/
├── pyproject.toml
├── README.md
├── .gitignore
├── run_cli.sh                 # human CLI launcher
│
├── src/
│   └── network_lab_mcp/
│       ├── __init__.py
│       ├── mcp_server.py      # stdio MCP server, defines the 7 tools
│       ├── lab.py             # running-config/topology/access-info/scenario/reference loading
│       ├── terminal.py        # tmux session management, ssh/telnet launch
│       ├── discovery.py       # topology discovery (LLDP/CDP + L3 enrichment)
│       │
│       └── cli/                       # human-facing CLI
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
│   │   └── sample.yaml        # tracked; every other file here is local/private, gitignored
│   │
│   └── references/
│       └── sample.yaml        # tracked; every other file here is local/private, gitignored
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
running-config's explicit `access-info <name>` / `topology <name>`
selections are the only real association between an access-info definition
and a topology; filenames are never matched to infer one.

### Sample lab

`lab/topologies/sample.yaml`, `lab/access-info/sample.yaml`,
`lab/scenarios/sample.yaml`, and `lab/references/sample.yaml` are tracked
in git and describe one small, coherent fictional lab (`R1`/`R2`, an
optional `jump1` jump host). The topology holds only safe logical data; the
access-info definition holds the matching fictional connection data, using
only documentation-only addresses from the RFC 5737 `192.0.2.0/24` range.
**These addresses are not reachable and must not be used as real
connectivity targets**, and the sample password (`example-password`) is not
a real credential — create your own private access-info for a real lab. The
samples exist to validate YAML loading, MCP structured output,
device-access resolution, and device-name/session-name mapping.

### Git safety design

This repository is meant to be shared publicly, but real access-info YAML
contains device names, management addresses, usernames, passwords, and
other private lab information; real scenario/reference files may also
describe private task or customer context. Rather than relying on
documentation alone, `.gitignore` provides a default technical guard: only
each concept's one fictional `sample.yaml` is tracked, and every other file
under `lab/topologies/`, `lab/access-info/`, `lab/scenarios/`, and
`lab/references/` is gitignored by default, regardless of its name.

```gitignore
lab/settings.yaml
lab/topologies/*.yaml
!lab/topologies/sample.yaml
lab/access-info/*.yaml
!lab/access-info/sample.yaml
lab/scenarios/*.yaml
!lab/scenarios/sample.yaml
lab/references/*.yaml
!lab/references/sample.yaml
```

This is **not** a complete security boundary — it is a default that lowers
the chance of accidentally committing real credentials or private lab data
to a public repository. Treat any access-info file that leaves this
repository as sensitive regardless of what git tracks. Topology YAML never
carries private access fields at all (`lab.validate_topology_data()`
rejects `address`/`transport`/`port`/`username`/`password` outright), so it
is a much lower-risk file even before considering `.gitignore`.

## Current limitations

- Topology discovery supports IOS XR (LLDP + CDP), IOS XE (LLDP + CDP), and
  classic IOS (CDP only): no NX-OS discovery, no SNMP/NETCONF/RESTCONF, and
  no generic discovery plugin framework.
- L3 topology enrichment deliberately stores only a directly observed IPv4
  address + VRF per interface: no prefix length, no subnet/link inference
  from addresses, and no operational state is ever persisted into topology.
- No automatic stale-link pruning after Discovery.
- `terminal_open()` automates SSH password authentication and classic-IOS-
  style Telnet username/password login only; it does not automate a
  host-key confirmation prompt, and `terminal_send()`/`terminal_read()`
  themselves remain a simple, unattended capture/send with no login
  automation of their own beyond that one-time `terminal_open()` step.
- Non-editable/wheel installation is not supported.
- access-info has no external-editor support (structured CLI editing only),
  unlike topology/scenario/reference.
- Single-hop OpenSSH ProxyJump only: a jump host cannot itself reference
  another jump host, and only `type: host` / `transport: ssh` jump hosts
  are supported.
- The case-only topology-name collision safeguard (`topology <name>`) is
  the only such safeguard; there is no equivalent for `device <name>` or
  for access-info/jump-host/scenario/reference names (identifiers remain
  fully case-sensitive regardless).
- Scenario/reference schema is intentionally not fixed — only "valid YAML,
  root is a mapping" is enforced (see
  [docs/scenario_format.md](docs/scenario_format.md)).

## MCP SDK

Network Lab MCP uses the official
[MCP Python SDK](https://pypi.org/project/mcp/), pinned as `mcp>=2.2,<3` in
`pyproject.toml`. It uses the SDK's `MCPServer` class over the `stdio`
transport. See [docs/mcp_tools.md](docs/mcp_tools.md) for the tool
reference.

## Version, license

Run `help` in the CLI (`./run_cli.sh`) for a Quick Start covering the
typical configuration workflow, and `show version` (EXEC mode only) for the
exact version, release date, source revision, and license currently
running. `help claude` explains how Claude Code uses Network Lab MCP.

Network Lab MCP is licensed under the **GNU General Public License v3.0**
(see [LICENSE](LICENSE)). Version, author, and license metadata are
declared once in `pyproject.toml` and read at runtime via
`importlib.metadata` — `show version`'s output and this README are the same
source, never two independently maintained copies.
