# CLI Reference

This is the reference for the Step 2 human-facing Network Lab CLI, launched
with `./run_cli.sh`. It is a human configuration/control plane for
`lab/settings.yaml` and `lab/topologies/*.yaml`, edited through a
candidate/commit model with IOS XR-compatible interaction. It does not speak
the MCP stdio protocol and is a separate process from `network-lab-mcp` —
see [architecture.md](architecture.md#step-2-the-human-configuration-control-plane).

The command grammar (`src/network_lab_mcp/cli/grammar.py`) is the single
source of truth for everything in this document: parsing, abbreviation,
completion, `?` help, and error reporting all walk the same trie.

## Modes and prompts

| Mode | Prompt |
|------|--------|
| EXEC | `network-lab#` |
| Global configuration | `network-lab(config)#` |
| Topology configuration | `network-lab(config-topology-<name>)#` |
| Device configuration | `network-lab(config-device-<name>)#` |

`<name>` is the exact, case-preserved topology/device identifier currently
selected.

## EXEC mode commands

| Command | Effect |
|---------|--------|
| `configure` (alias: `configure terminal`) | Enter global configuration mode; initializes a settings candidate (see "Candidate initialization" below). |
| `show running-config` | Show the committed lab configuration (topology, scenario, references) as currently on disk. |
| `help` | Display the commands valid at the current position (same content as bare `?`). |
| `exit` / `quit` | Terminate the CLI process. Only reachable in EXEC mode, where by construction no candidate configuration exists. |

## Global configuration mode commands

| Command | Effect |
|---------|--------|
| `topology <name>` | Select or create a topology candidate (see below); enters topology configuration mode. |
| `scenario <name>` | Set the candidate active scenario. `<name>` must exactly match an existing file under `lab/scenarios/`. |
| `reference <name>` | Add `<name>` to the candidate active references. `<name>` must exactly match an existing file under `lab/references/`; duplicates are rejected. |
| `no reference <name>` | Remove `<name>` from the candidate active references. |
| `show configuration` | Show the current candidate (settings candidate, plus the topology candidate if one is selected). |
| `show running-config` | Show the committed configuration on disk (unaffected by the candidate). |
| `commit` | Validate and persist the candidate; return to EXEC. |
| `abort` | Discard the entire candidate (including any topology edit); return to EXEC. No disk writes. |
| `end` | Return to EXEC. Blocked (with a warning) if any candidate scope is dirty. Never implicitly commits or aborts. |
| `exit` | Same as `end` at this mode: return to EXEC, blocked while dirty. |
| `help` / `?` | Display the commands valid here. |

## Topology configuration mode commands

| Command | Effect |
|---------|--------|
| `description <text>` | Set the topology's free-form description (rest-of-line argument). |
| `device <name>` | Select or create a device (exact case, case-sensitive) in the topology candidate; enters device configuration mode. |
| `show configuration` / `show running-config` | As above. |
| `commit` / `abort` | As above (topology-scoped edits are part of the same overall candidate). |
| `end` | Return directly to EXEC; blocked while dirty. |
| `exit` | Return one level up, to global configuration mode. No dirty check (still the same configuration session). |
| `help` / `?` | Display the commands valid here. |

## Device configuration mode commands

| Command | Effect |
|---------|--------|
| `type <iosxr\|iosxe\|nxos\|host>` | Device type. A fixed, closed enum (Cisco IOS XR / IOS XE / NX-OS, or `host` for a generic host/endpoint) rather than a free-form value; unique-prefix abbreviation is accepted (e.g. `nx` -> `nxos`, `h` -> `host`) and the committed value is always normalized to lowercase. Rejected outright if not one of the four. Validated immediately, like `transport`. Reserved for Step 3 topology discovery to dispatch platform-specific CDP/LLDP commands and parsers -- `host` marks a registered topology node that discovery will intentionally skip, not an unsupported type. |
| `address <value>` | Device management address. |
| `transport <ssh\|telnet>` | Device transport. Validated immediately (invalid input is rejected with a caret, not accepted into the candidate). |
| `port <1-65535>` | Device port. Validated immediately. |
| `username <value>` | Device username. |
| `password <value>` | Device password. Stored in plain text like Step 1 (this is a lab tool, not a secret manager) but never displayed, completed, or retained in history — see "Password safety" below. |
| `no username` / `no password` / `no port` | Clear the corresponding field. |
| `show configuration` / `show running-config` | As above. |
| `commit` / `abort` | As above. |
| `end` | Return directly to EXEC; blocked while dirty. |
| `exit` | Return one level up, to topology configuration mode. |
| `help` / `?` | Display the commands valid here. |

Device identifier lookup/selection is case-sensitive (`device R1` and
`device r1` are different devices).

## Example session

```
network-lab# conf
network-lab(config)# top srv6_lab
network-lab(config-topology-srv6_lab)# dev R1
network-lab(config-device-R1)# type iosxr
network-lab(config-device-R1)# address 192.168.1.11
network-lab(config-device-R1)# tra ssh
network-lab(config-device-R1)# port 22
network-lab(config-device-R1)# username cisco
network-lab(config-device-R1)# password cisco
network-lab(config-device-R1)# exit
network-lab(config-topology-srv6_lab)# exit
network-lab(config)# scenario troubleshoot
network-lab(config)# reference iosxr_operational
network-lab(config)# reference srv6
network-lab(config)# show configuration
network-lab(config)# commit
Commit complete.
network-lab#
```

## Unique fixed-keyword abbreviation

Any *fixed keyword* (`configure`, `topology`, `scenario`, `reference`,
`device`, `transport`, `show`, `commit`, `abort`, `exit`, `end`, `no`,
`help`, `username`, `password`, `port`, `address`, `type`, `description`,
`running-config`, `configuration`) can be abbreviated to any prefix that is
*unique at the current grammar position*:

```
conf            -> configure
top srv6_lab    -> topology srv6_lab
dev R1          -> device R1
tra ssh         -> transport ssh
```

A prefix matching more than one keyword at the current position is
**ambiguous** and is rejected outright — nothing is guessed or executed:

```
network-lab(config)# s
% Ambiguous command: "s"
```

(In global configuration mode, `s` matches both `scenario` and `show`.) Use
Tab or `?` to see the candidates. Object identifiers (topology/scenario/
reference/device names) are never abbreviated this way — see below.

## Fixed-keyword case-insensitivity vs. object-identifier case-sensitivity

Fixed CLI keywords are matched **case-insensitively**: `configure`,
`CONFIGURE`, and `Configure` are equivalent, as are nested keywords like
`TRANSPORT` or `Device`. Documentation and displayed help always show the
lowercase form.

Object identifiers — **topology, scenario, reference, and device names** —
are matched **case-sensitively** and are never case-folded:

```
topology srv6_lab      -> selects the existing topology named exactly srv6_lab
topology SRv6_Lab       -> does NOT silently select srv6_lab (see below)

scenario failover_test  -> matches an existing scenario named exactly that
scenario Failover_Test  -> a normal "does not exist" lookup failure, nothing is created

device R1               -> matches
device r1                -> a different device (or a new one), never R1
```

A wrong-case `scenario`/`reference` is a normal exact-identifier lookup
failure (`% Scenario '...' does not exist.` / `% Reference '...' does not
exist.`) — nothing is silently matched or created.

## Case-only topology-name collision safeguard

`topology <name>` is special because it can both *select* an existing
topology and *implicitly create* a new one. If the entered name does not
exactly match an existing topology, but differs from one **only by letter
case** (nothing else — not `-` vs. `_`, not any added/removed character),
the CLI requires explicit confirmation before creating a distinct topology:

```
network-lab(config)# topology SRv6_Lab

% An existing topology 'srv6_lab' differs only by letter case.
Create a separate topology named 'SRv6_Lab'? [yes/no]:
```

- An empty answer or anything other than `y`/`yes` is treated as **no**: no
  topology candidate is created or changed, the candidate active topology is
  left exactly as it was, and the CLI remains in global configuration mode.
- Answering `yes` creates a new topology candidate under the **exact
  entered case** (`SRv6_Lab`, never rewritten to `srv6_lab`) and enters
  topology configuration mode for it, leaving the existing `srv6_lab`
  completely untouched.

This is a narrow safety net, not fuzzy name matching: `srv6-lab`,
`srv6lab`, and `srv_lab` are all ordinary new-topology names relative to an
existing `srv6_lab` and never trigger this prompt.

## Tab / Ctrl-I completion

Tab (and Ctrl-I, which is the same key at the terminal protocol level)
performs context-sensitive completion of the token under the cursor,
without executing the command (Enter is always required separately):

- **Unique match** (fixed keyword or a single matching dynamic candidate):
  completes the current token in place.
- **Multiple matches**: the current input is left untouched and the
  candidate list is printed below the prompt, which is then redisplayed
  with the original input intact — nothing is guessed.
- **Zero matches**: the input buffer is left untouched; nothing is executed;
  a terminal bell may sound.

Dynamic completion is available for:

| Position | Candidates |
|----------|------------|
| `topology <name>` | Existing topology names on disk (exact stored case) |
| `scenario <name>` | Existing scenario names on disk |
| `reference <name>` | Existing reference names on disk |
| `no reference <name>` | The candidate's *currently active* reference names |
| `device <name>` | Devices already in the selected topology candidate |
| `transport <value>` | `ssh`, `telnet` |
| `type <value>` | `iosxr`, `iosxe`, `nxos`, `host` |

Object-identifier completion is case-sensitive and always displays the
exact stored case:

```
device R<Tab>   -> R1 / R2 (if both exist)
device r<Tab>   -> no match on R1/R2 purely by case folding

topology s<Tab> -> may complete srv6_lab
topology S<Tab> -> does not match srv6_lab merely by case folding
```

Free-form fields (`address`, `username`, `description`) are never
completion candidates. **`password` never offers completion candidates or
reveals a value**, at any position (`password <Tab>` or `password
partial<Tab>` — both a no-op beyond an optional bell).

## Context-sensitive `?`

`?` is a single key press — it never inserts a literal `?` character and
never requires Enter.

- **Bare `?`** (empty line, or right after a space): lists every valid next
  token at the current position, then redisplays the prompt with the input
  unchanged.
- **Partial-token `?`** (immediately after a partial fixed keyword, no
  space): lists only the fixed keywords that still match the partial input.
- **Next-token `?`** (after a keyword and a space): lists the next
  syntax — a generic hint (e.g. `<name>` for an identifier argument) when
  nothing has been typed yet, or matching dynamic candidates once a prefix
  has been typed for an identifier argument, or the full small enum list
  (e.g. `ssh`/`telnet` for `transport`) even with nothing typed.
- **`<cr>`**: shown whenever the current position is already a complete,
  executable command (e.g. `commit ?` shows only `<cr>`).

```
network-lab# ?
  configure          Enter configuration mode
  show               Show information
  help               Display help
  exit               Exit the CLI
  quit               Exit the CLI

network-lab# con?
  configure

network-lab(config-device-R1)# transport ?
  ssh                 Use SSH transport
  telnet              Use Telnet transport

network-lab(config)# topology ?
  <name>              Topology name

network-lab(config)# commit ?
  <cr>
```

Object-identifier candidates shown through `?` preserve exact stored case,
the same as Tab completion. `password ?` shows only the generic
`<password>` hint — never a real value or candidate list.

## Command history

In-process-only command history (never written to disk, and gone once the
CLI exits):

- Up Arrow / Ctrl-P: previous command
- Down Arrow / Ctrl-N: next/newer command

A `password`-setting command (including an abbreviated form like `pas
cisco123`) is never added to this in-memory history, so it can never be
recalled with Up Arrow either.

## Line editing

Standard Emacs/IOS XR-style editing:

| Key | Effect |
|-----|--------|
| Left / Ctrl-B | Cursor left |
| Right / Ctrl-F | Cursor right |
| Ctrl-A | Cursor to beginning of line |
| Ctrl-E | Cursor to end of line |
| Backspace | Delete previous character |
| Ctrl-D (non-empty line) | Delete character at cursor |
| Ctrl-W | Delete previous word |
| Ctrl-U | Delete from cursor to beginning of line |
| Ctrl-K | Delete from cursor to end of line |

## Ctrl-C: cancel the input line only

Ctrl-C **never** terminates the CLI process and **never** discards candidate
configuration. It cancels only the currently-typed, not-yet-submitted input
line and redisplays a fresh prompt in the same mode:

```
network-lab(config-device-R1)# address 192.0.2.<Ctrl-C>
network-lab(config-device-R1)#
```

Everything committed to the candidate *before* that keystroke (including
edits made earlier in the same session) is untouched. Pressing Ctrl-C
repeatedly has the same effect each time — it is never treated as a request
to exit or to discard the candidate; use `exit`/`end`/`abort`/`quit` or
Ctrl-D for that.

## Ctrl-D / EOF

- **Non-empty input line**: deletes the character at the cursor (standard
  line editing, not EOF).
- **Empty input line**: requests to exit the whole CLI process, subject to
  the same uncommitted-changes guard as `exit`/`end`:
  - If `overall_dirty` is true (any candidate scope has uncommitted
    changes), the request is refused, a warning is printed, the candidate is
    left completely intact, and the CLI keeps running in the same mode.
  - If clean, the CLI process exits normally.

```
network-lab(config)# <Ctrl-D on empty line, with a pending scenario change>
% Uncommitted changes exist. Use 'commit' or 'abort'.
network-lab(config)#
```

## Candidate configuration model

```
committed_settings   <- lab/settings.yaml, snapshotted on `configure`
settings_candidate    <- editable copy, mutated by topology/scenario/reference commands

selected_topology_name
topology_original     <- None for a brand-new topology, otherwise the loaded snapshot
topology_candidate    <- editable copy
```

Everything above is memory-only inside the CLI process. Nothing under
`lab/` is written until `commit` succeeds.

### Candidate initialization

`configure` snapshots committed settings into a settings candidate only. The
full active topology is **not** automatically loaded — `show configuration`
works immediately (showing just the candidate active topology name,
scenario, and references, with a note that no topology edit context is
selected yet), and no topology YAML is read or rewritten until a `topology
<name>` command is issued.

### Scoped dirty state

- `settings_dirty` — the settings candidate differs from the committed
  settings snapshot.
- `topology_dirty` — an existing topology candidate differs from what was
  loaded from disk.
- `new_topology_dirty` — the selected topology does not exist on disk yet.
- `overall_dirty` — the logical OR of the three; this is what guards
  `exit`/`end`/Ctrl-D.

A settings-only change (e.g. `scenario troubleshoot`) does **not** block
switching to a different topology. An in-progress topology edit **does**
block switching until `commit` or `abort`:

```
network-lab(config-topology-lab_a)# description changed
network-lab(config-topology-lab_a)# exit
network-lab(config)# topology lab_b
% Uncommitted topology changes exist. Use 'commit' or 'abort' before switching topology.
```

A *clean* topology selection (loaded, but never semantically changed) can
be freely switched away from.

## `commit`

1. Validates the entire candidate: the active topology exists (or is being
   created/edited in this same commit), the active scenario and every
   active reference exist as exact-case files on disk, and — if a topology
   is being written — it passes the Step 1 topology/device validator
   (`network_lab_mcp.lab.validate_topology_data()`, built on
   `validate_topology_device_names()`). No duplicate validation logic is
   maintained in the CLI.
2. On any validation failure: **zero disk writes**, the candidate and
   current mode are retained unchanged, and every failure is printed as a
   `% ...` line.
3. On success, only the scopes that were actually dirty are written
   (settings and/or topology YAML), using an atomic write (temp file,
   flush, `os.replace()`); an unchanged topology that was only selected is
   never rewritten, and a fully clean commit ("No changes to commit.")
   writes nothing at all.
4. The CLI returns to EXEC mode on any commit outcome (success or no-op).

## `abort`

Discards the settings candidate and any topology candidate/edit
unconditionally, performs no disk writes, and returns to EXEC.

## `exit` / `end`

- In device mode, `exit` returns one level up to topology mode; no dirty
  guard (still the same overall configuration session).
- In topology mode, `exit` returns one level up to global configuration
  mode; no dirty guard.
- In global configuration mode, `exit` and `end` are equivalent: both
  return to EXEC, guarded by `overall_dirty`.
- `end` from topology or device mode jumps directly to EXEC, guarded by
  `overall_dirty`. `end` is never an implicit commit or abort.

## `show running-config` vs. `show configuration`

- `show running-config` — the **committed** configuration currently on
  disk, unaffected by any candidate. Available in every mode.
- `show configuration` — the **candidate** configuration: the settings
  candidate, plus the topology candidate if one is selected. Available in
  every mode except EXEC (there is no candidate in EXEC).

Both mask device passwords as `********` and display topology/scenario/
reference/device identifiers using their exact stored/candidate case.

## Password safety

A device `password` is stored in plain text in topology YAML, exactly like
Step 1 (this is a lab tool, not a secret manager). It is never shown by
`show configuration`/`show running-config`, never offered as a Tab/`?`
candidate or value, and never retained in the CLI's in-memory command
history — see "Command history" and "Tab / Ctrl-I completion" above.

## Limitations

- No `no topology <name>` (topology deletion is Step 3).
- No link editor; an existing topology's `links` (and any other field the
  CLI does not directly edit) are preserved untouched through a commit.
- Scenario and reference content is not editable from the CLI — selection
  only (see [scenario_format.md](scenario_format.md)).
- `lab/principles.yaml` is not edited by the CLI.
- The case-only collision safeguard is implemented for `topology <name>`
  only; `device <name>` has no equivalent safeguard (device identifiers
  remain fully case-sensitive, but a near-duplicate by case is not flagged).
- Candidate configuration is memory-only: it is not recoverable after an
  abrupt process termination (SIGKILL, crash, host failure). Normal exit
  paths (`exit`, `end`, `abort`, `quit`, Ctrl-D) never lose it unexpectedly.
