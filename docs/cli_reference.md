# CLI Reference

This is the reference for the Step 2 / 2.5 human-facing Network Lab CLI,
launched with `./run_cli.sh`. It is a human configuration/control plane for
running-config (`lab/settings.yaml`) and topology/access-info/scenario/
reference *definitions*, edited through a candidate/commit model with IOS
XR-compatible interaction. It does not speak the MCP stdio protocol and is a
separate process from `network-lab-mcp` — see
[architecture.md](architecture.md#step-2--25-the-human-configurationcontrol-plane).

The command grammar (`src/network_lab_mcp/cli/grammar.py`) is the single
source of truth for everything in this document: parsing, abbreviation,
completion, `?` help, and error reporting all walk the same trie.

## Configuration model in one line

- **running-config** (`running` mode): *which* topology/scenario/references
  MCP currently uses — a selection.
- **topology** / **access-info** / **scenario** / **reference** (global
  mode's `topology`/`access-info`/`scenario`/`reference <name>`): create or
  edit a *definition*. Never changes the running-config selection.

This replaces the original Step 2 model, where global `topology`/
`scenario`/`reference <name>` directly changed the active selection. That
selector semantics no longer exists anywhere in this CLI.

## Modes and prompts

| Mode | Prompt |
|------|--------|
| EXEC | `network-lab#` |
| Global configuration | `network-lab(config)#` |
| Running-config selection | `network-lab(config-running)#` |
| Topology definition | `network-lab(config-topology-<name>)#` |
| Topology device (safe fields) | `network-lab(config-device-<name>)#` |
| Access-info definition | `network-lab(config-access-info-<name>)#` |
| Access-info device (private fields) | `network-lab(config-access-device-<name>)#` |
| Scenario definition | `network-lab(config-scenario-<name>)#` |
| Reference definition | `network-lab(config-reference-<name>)#` |

`<name>` is the exact, case-preserved identifier currently open for editing.

## `?` vs. `help`: syntax help vs. Quick Start/usage help

These two are deliberately different, and neither one replaced the other:

- **`?`** — IOS XR-style **context-sensitive command syntax help**: "what
  can I type here?" Unaffected by anything in this section; see
  "Context-sensitive `?`" below.
- **`help`** — Network Lab MCP's own **Quick Start/usage help**: "how do I
  use Network Lab MCP?" Bare `help` shows an overview (purpose, the typical
  workflow, the five configuration areas, editor resolution order, and a
  pointer to `?` and to the topics below); it does **not** repeat the
  command list bare `?` already shows.
- **`help <topic>`** goes deeper on one subject. Topics are a fixed set,
  the same "select from an enum" pattern as `device.type`/`transport`, so
  `help ?` lists them and `<cr>`:

  ```
  network-lab# help ?
    claude               Show Claude Code integration help
    workflow             Show the recommended Network Lab workflow
    editor               Show external YAML editor usage
    cli                  Show CLI usage information
    <cr>
  ```

  | Topic | Covers |
  |-------|--------|
  | `help claude` | How Claude Code uses Network Lab MCP: the seven MCP tools, what Claude does/doesn't receive, device addressing by logical ID, and how to register/start Claude Code. |
  | `help workflow` | The recommended end-to-end sequence: access-info -> topology -> scenario -> reference -> running-config -> review -> commit -> use Claude Code. |
  | `help editor` | External YAML editor usage: resolution order, candidate-only editing, when `commit`/`clear` apply. |
  | `help cli` | A short cheat sheet for `?`, Tab, `configure`, `show running-config`/`show configuration`, `commit`, `clear`, `exit`, `end`, Ctrl-C — not a substitute for this document. |

`help` and `help <topic>` are available in every mode (EXEC and every
configuration mode) with identical content — they answer a question about
Network Lab MCP itself, not about the current grammar position, so they do
not vary by mode the way `?` does. Both are read-only: neither touches
candidate state, dirty state, or any file.

## `show version`

`show version` is a read-only software-information command, available in
every mode, that never touches candidate/dirty state, never reads
access-info, and does not depend on the active topology/scenario/reference:

```
network-lab# show version
Network Lab MCP

  Version:       0.1.0
  Release date:  2026-09-20
  Git commit:    abc1234
  Author:        Eitaro Uemura
  License:       GNU General Public License v3.0
  Python:        3.12.3
network-lab#
```

- **Version**, **Author**, and **License** are read from installed package
  metadata (`pyproject.toml` is their single source of truth, via
  `importlib.metadata`) — never a second hard-coded copy.
- **Release date** is a small constant in `network_lab_mcp/__init__.py`
  (`__release_date__`), since standard packaging metadata has no field for
  it; it is a separate concept from **Version** (a release date is not a
  version identifier).
- **Git commit** is resolved dynamically (`git rev-parse --short HEAD`
  against the repository checkout) every time the command runs — it is
  never hard-coded to whatever revision happened to be current when a
  feature was written. If `git` is unavailable, the checkout is not a git
  repository, or resolution fails for any other reason, this prints
  `unavailable` instead of failing the command or leaking a raw git error.
- **Python** is `platform.python_version()` of the interpreter actually
  running the CLI — never a hard-coded version string.

## EXEC mode commands

| Command | Effect |
|---------|--------|
| `configure` (alias: `configure terminal`) | Enter global configuration mode; initializes a running-config candidate (see "Candidate model" below). |
| `show running-config` | Show the committed MCP definition selection (topology/scenario/reference names) — never a definition's own content. Identical in every mode; see "`show running-config` vs. `show configuration`". |
| `show version` | Show Network Lab MCP's own version/license/runtime information — see "`show version`" above. |
| `help` / `help <topic>` | Network Lab MCP Quick Start/usage help — see "`?` vs. `help`" above. Not the same as bare `?`. |
| `exit` / `quit` | Terminate the CLI process. Only reachable in EXEC mode, where by construction no candidate configuration exists. |

## Global configuration mode commands

| Command | Effect |
|---------|--------|
| `running-config` | Enter running-config selection mode (`network-lab(config-running)#`). |
| `access-info <name>` | Create or edit an access-info definition; `<name>` existing loads it, otherwise starts a new one. Enters access-info definition mode. |
| `topology <name>` | Create or edit a topology definition (see "Case-only topology-name collision safeguard" below). Enters topology definition mode. |
| `scenario <name>` | Create or edit a scenario definition. Enters scenario definition mode. |
| `reference <name>` | Create or edit a reference definition. Enters reference definition mode. |
| `show configuration` | Show whichever definition candidate (if any) is currently open — a note is shown if none is. |
| `show running-config` | As above (committed selection, not the open definition). |
| `clear` | Discard the entire uncommitted configure-session state (running-config candidate and any open definition candidate); stay in the current mode (or the nearest still-valid parent — see "`clear`" below). |
| `commit` | Validate and persist the candidate; return to EXEC. |
| `end` | Return to EXEC. Blocked (with a warning) if any candidate scope is dirty. Never implicitly commits or clears. |
| `exit` | Same as `end` at this mode: return to EXEC, blocked while dirty. |
| `show version` / `help` / `help <topic>` / `?` | As in EXEC mode — see above. |

Opening a *different* definition (of any kind) while the current one is
dirty is blocked, the same way switching topologies was guarded in the
original Step 2 model — see "Definition switching guard" below.

`show version`, `help`, and `help <topic>` behave identically in every
mode (see above); the per-mode tables below omit them for brevity and list
only what differs from EXEC/global.

## Running-config selection mode commands

| Command | Effect |
|---------|--------|
| `topology <name>` | Select the topology MCP will use. `<name>` must already exist as a committed topology, *or* be the topology currently being created/edited in this same configure session. |
| `scenario <name>` | Select the scenario MCP will use. Same existence rule as `topology`. |
| `reference <name>` | Add `<name>` to the selected references. Same existence rule; duplicates are rejected. |
| `no reference <name>` | Remove `<name>` from the selected references. |
| `show configuration` | Show the running-config **candidate** (this mode's own pending selection). |
| `show running-config` | Show the committed selection on disk (unaffected by the candidate). |
| `clear` / `commit` / `end` / `exit` / `help` | As in global configuration mode. `exit` returns one level up, to global configuration mode (no dirty guard — still the same overall configure session). |

## Topology definition mode commands

| Command | Effect |
|---------|--------|
| `description <text>` | Set the topology's free-form description (rest-of-line argument). |
| `device <name>` | Create or edit a device's *safe* metadata (case-sensitive); enters topology device mode. |
| `edit` | Open the topology candidate in an external YAML editor (see "External YAML editor" below). |
| `show configuration` / `show running-config` | As in global mode. |
| `clear` / `commit` | As above (topology-scoped edits are part of the same overall candidate). |
| `end` | Return directly to EXEC; blocked while dirty. |
| `exit` | Return one level up, to global configuration mode. No dirty check (still the same configuration session). |
| `show version` / `help` / `help <topic>` / `?` | As in EXEC mode. |

## Topology device mode commands (safe fields only)

| Command | Effect |
|---------|--------|
| `type <iosxr\|iosxe\|nxos\|host>` | Device type. See "Device type enum" below. |
| `show configuration` / `show running-config` | As above. |
| `clear` / `commit` / `end` / `exit` / `help` | As above. `exit` returns one level up, to topology definition mode. |

Topology device mode intentionally has **no** `address`/`transport`/`port`/
`username`/`password` — those private connection fields live under
access-info device mode instead, since topology is exposed to Claude via
`get_active_topology()` and must never carry them.

## Access-info definition mode commands

| Command | Effect |
|---------|--------|
| `device <name>` | Create or edit a device's private connection data (case-sensitive); enters access-info device mode. |
| `show configuration` / `show running-config` | As above (candidate rendering masks `password`). |
| `clear` / `commit` / `end` / `exit` / `help` | As above. `exit` returns one level up, to global configuration mode. |

access-info has no `edit` command in this phase — see
["Why access-info has no external editor yet"](#why-access-info-has-no-external-editor-yet).

## Access-info device mode commands (private connection fields)

| Command | Effect |
|---------|--------|
| `type <iosxr\|iosxe\|nxos\|host>` | Device type. See "Device type enum" below; validated against the same SSOT as topology device mode. |
| `address <value>` | Device management address. |
| `transport <ssh\|telnet>` | Device transport. Validated immediately (invalid input is rejected with a caret, not accepted into the candidate). |
| `port <1-65535>` | Device port. Validated immediately. |
| `username <value>` | Device username. |
| `password <value>` | Device password. Stored in plain text like Step 1 (this is a lab tool, not a secret manager) but never displayed, completed, or retained in history — see "Password safety" below. |
| `no username` / `no password` / `no port` | Clear the corresponding field. |
| `show configuration` / `show running-config` | As above. |
| `clear` / `commit` / `end` / `exit` / `help` | As above. `exit` returns one level up, to access-info definition mode. |

## Scenario / reference definition mode commands

| Command | Effect |
|---------|--------|
| `edit` | Open the candidate in an external YAML editor (see below). |
| `show configuration` | Show the candidate as YAML (schema is intentionally not fixed yet — see [scenario_format.md](scenario_format.md)). |
| `show running-config` | As above (committed selection, not this definition's content). |
| `clear` / `commit` / `end` / `exit` / `help` | As above. `exit` returns one level up, to global configuration mode. |

Device identifier lookup/selection is case-sensitive (`device R1` and
`device r1` are different devices), and so is every other definition name.

## Example session

```
network-lab# conf
network-lab(config)# top srv6_lab
network-lab(config-topology-srv6_lab)# dev R1
network-lab(config-device-R1)# type iosxr
network-lab(config-device-R1)# exit
network-lab(config-topology-srv6_lab)# exit
network-lab(config)# access-info srv6_lab
network-lab(config-access-info-srv6_lab)# device R1
network-lab(config-access-device-R1)# type iosxr
network-lab(config-access-device-R1)# address 192.168.1.11
network-lab(config-access-device-R1)# tra ssh
network-lab(config-access-device-R1)# port 22
network-lab(config-access-device-R1)# username cisco
network-lab(config-access-device-R1)# password cisco
network-lab(config-access-device-R1)# exit
network-lab(config-access-info-srv6_lab)# exit
network-lab(config)# scenario troubleshoot
network-lab(config-scenario-troubleshoot)# edit
network-lab(config-scenario-troubleshoot)# exit
network-lab(config)# running-config
network-lab(config-running)# topology srv6_lab
network-lab(config-running)# scenario troubleshoot
network-lab(config-running)# reference iosxr_operational
network-lab(config-running)# show configuration
network-lab(config-running)# commit
Commit complete.
network-lab#
```

## Unique fixed-keyword abbreviation

Any *fixed keyword* (`configure`, `running-config`, `access-info`,
`topology`, `scenario`, `reference`, `device`, `transport`, `show`, `clear`,
`commit`, `exit`, `end`, `no`, `help`, `edit`, `username`, `password`,
`port`, `address`, `type`, `description`, `configuration`) can be
abbreviated to any prefix that is *unique at the current grammar position*:

```
conf            -> configure
top srv6_lab    -> topology srv6_lab
dev R1          -> device R1
tra ssh         -> transport ssh
```

A prefix matching more than one keyword at the current position is
**ambiguous** and is rejected outright — nothing is guessed or executed:

```
network-lab(config)# r
% Ambiguous command: "r"
```

(In global configuration mode, `r` matches both `running-config` and
`reference`.) Use Tab or `?` to see the candidates. Object identifiers
(topology/scenario/reference/access-info/device names) are never abbreviated
this way — see below.

## Fixed-keyword case-insensitivity vs. object-identifier case-sensitivity

Fixed CLI keywords are matched **case-insensitively**: `configure`,
`CONFIGURE`, and `Configure` are equivalent, as are nested keywords like
`TRANSPORT` or `Device`. Documentation and displayed help always show the
lowercase form.

Object identifiers — **topology, scenario, reference, access-info, and
device names** — are matched **case-sensitively** and are never case-folded:

```
topology srv6_lab      -> selects/edits the existing topology named exactly srv6_lab
topology SRv6_Lab       -> does NOT silently select srv6_lab (see below)

device R1               -> matches
device r1                -> a different device (or a new one), never R1
```

Under `running-config` mode, a wrong-case `scenario`/`reference`/`topology`
selector is a normal exact-identifier lookup failure (`% Scenario '...' does
not exist.`) — nothing is silently matched or created.

## Case-only topology-name collision safeguard

Global `topology <name>` is special because it can both *open* an existing
topology definition and *implicitly create* a new one. If the entered name
does not exactly match an existing topology, but differs from one **only by
letter case** (nothing else — not `-` vs. `_`, not any added/removed
character), the CLI requires explicit confirmation before creating a
distinct topology:

```
network-lab(config)# topology SRv6_Lab

% An existing topology 'srv6_lab' differs only by letter case.
Create a separate topology named 'SRv6_Lab'? [yes/no]:
```

- An empty answer or anything other than `y`/`yes` is treated as **no**: no
  topology candidate is created or changed, and the CLI remains in global
  configuration mode.
- Answering `yes` creates a new topology candidate under the **exact
  entered case** (`SRv6_Lab`, never rewritten to `srv6_lab`) and enters
  topology definition mode for it, leaving the existing `srv6_lab`
  completely untouched.

This is a narrow safety net, not fuzzy name matching: `srv6-lab`,
`srv6lab`, and `srv_lab` are all ordinary new-topology names relative to an
existing `srv6_lab` and never trigger this prompt. It applies only to
`topology <name>`; access-info/scenario/reference names have no equivalent
safeguard.

## Definition switching guard

Opening a definition (`access-info`/`topology`/`scenario`/`reference
<name>`) while a *different* definition is already open and dirty is
blocked:

```
network-lab(config-topology-lab_a)# description changed
network-lab(config-topology-lab_a)# exit
network-lab(config)# topology lab_b
% Uncommitted changes exist. Use 'commit' or 'clear' before switching.
```

A *clean* definition (loaded, but never semantically changed) can be freely
switched away from. Running-config editing is an independent scope: opening
`running-config` mode, or switching what it selects, is never blocked by a
dirty definition candidate, and vice versa.

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
| `topology <name>` (global) | Existing topology names on disk |
| `access-info <name>` | Existing access-info definition names on disk |
| `scenario <name>` (global) | Existing scenario names on disk |
| `reference <name>` (global) | Existing reference names on disk |
| `topology <name>` (running) | Existing topology names on disk |
| `scenario <name>` (running) | Existing scenario names on disk |
| `reference <name>` (running) | Existing reference names on disk |
| `no reference <name>` | The running-config candidate's *currently selected* reference names |
| `device <name>` (topology) | Devices already in the open topology candidate |
| `device <name>` (access-info) | Devices already in the open access-info candidate |
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
never requires Enter, never touches candidate/mode/history state on its
own, and preserves the input buffer and cursor position. Pressing it
echoes a `prompt + buffer + ?` transcript line into scrollback (exactly what
was typed, the same way a real terminal leaves a record of the keypress),
then the matching help lines, then redisplays the same prompt and buffer so
you can keep typing immediately:

```
network-lab# show ?
  running-config       Show committed MCP definition selection
network-lab# show
```

Repeated `?` presses each leave their own transcript + help block — that is
normal scrollback, not a bug:

```
network-lab# show running-config ?
  <cr>
network-lab# show running-config ?
  <cr>
network-lab# show running-config
```

- **Bare `?`** (empty line, or right after a space): lists every valid next
  token at the current position.
- **Partial-token `?`** (immediately after a partial fixed keyword, no
  space): lists only the fixed keywords that still match the partial input.
- **Next-token `?`** (after a keyword and a space): lists the next
  syntax — a generic hint (e.g. `<name>` for a plain selector argument, such
  as `running-config` mode's `topology <name>`) when nothing has been typed
  yet, matching dynamic candidates once a prefix has been typed for an
  identifier argument, the full small enum list (e.g. `ssh`/`telnet` for
  `transport`, or the four `device.type` values) even with nothing typed, or
  — for a **"select or create"** identifier such as global mode's
  `topology`/`access-info`/`scenario`/`reference <name>`, or `device <name>`
  under topology/access-info — every existing name *plus* a creation hint,
  with nothing typed yet:

  ```
  network-lab(config)# topology ?
    sample_lab           Existing topology
    test                 Existing topology
    <name>               Create or edit topology
  ```

- **`<cr>`**: shown whenever the current position is already a complete,
  executable command (e.g. `commit ?` shows only `<cr>`).

```
network-lab# ?
  configure          Enter configuration mode
  show               Show information
  help               Display Network Lab MCP quick start help
  exit               Exit the CLI
  quit               Exit the CLI

network-lab# con?
  configure

network-lab(config-access-device-R1)# transport ?
  ssh                 Use SSH transport
  telnet              Use Telnet transport

network-lab(config-running)# topology ?
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
configuration (it is not `clear`). It cancels only the currently-typed,
not-yet-submitted input line and redisplays a fresh prompt in the same mode:

```
network-lab(config-access-device-R1)# address 192.0.2.<Ctrl-C>
network-lab(config-access-device-R1)#
```

Everything committed to either candidate scope *before* that keystroke
(including edits made earlier in the same session) is untouched. Pressing
Ctrl-C repeatedly has the same effect each time — it is never treated as a
request to exit or to discard the candidate; use `exit`/`end`/`clear`/`quit`
or Ctrl-D for that.

## Ctrl-D / EOF

- **Non-empty input line**: deletes the character at the cursor (standard
  line editing, not EOF).
- **Empty input line**: requests to exit the whole CLI process, subject to
  the same uncommitted-changes guard as `exit`/`end`:
  - If `overall_dirty` is true (either candidate scope has uncommitted
    changes), the request is refused, a warning is printed, the candidate is
    left completely intact, and the CLI keeps running in the same mode.
  - If clean, the CLI process exits normally.

```
network-lab(config)# <Ctrl-D on empty line, with a pending scenario change>
% Uncommitted changes exist. Use 'commit' or 'clear'.
network-lab(config)#
```

## Candidate model

```
committed_settings   <- lab/settings.yaml, snapshotted on `configure`
settings_candidate    <- editable copy, mutated only from `running` mode

definition_kind       <- "topology" | "access_info" | "scenario" | "reference" | None
definition_name
definition_original   <- None for a brand-new definition, otherwise the loaded snapshot
definition_candidate  <- editable copy
```

Everything above is memory-only inside the CLI process. Nothing under
`lab/` is written until `commit` succeeds. At most one definition is open
at a time; the running-config candidate and the open definition candidate
are dirtied, switched, and cleared independently of each other.

### Candidate initialization

`configure` snapshots committed running-config into a settings candidate
only. No definition is automatically loaded — `show configuration` works
immediately (showing a note that no definition is currently selected for
editing), and no definition YAML is read or rewritten until a
`topology`/`access-info`/`scenario`/`reference <name>` command is issued.

### Scoped dirty state

- `settings_dirty` — the running-config candidate differs from the
  committed settings snapshot.
- `definition_dirty` — the open definition candidate differs from what was
  loaded from disk (or, for a brand-new definition, is always dirty until
  committed).
- `overall_dirty` — the logical OR of the two; this is what guards
  `exit`/`end`/Ctrl-D.

A running-config-only change (e.g. a pending `scenario` selection) does
**not** block opening or switching a definition, and vice versa. An
in-progress definition edit **does** block opening a *different* definition
until `commit` or `clear`:

```
network-lab(config-topology-lab_a)# description changed
network-lab(config-topology-lab_a)# exit
network-lab(config)# topology lab_b
% Uncommitted changes exist. Use 'commit' or 'clear' before switching.
```

A *clean* definition (loaded, but never semantically changed) can be freely
switched away from.

## `commit`

1. Validates the entire candidate: if a definition is open and dirty, it is
   validated with the matching SSOT validator for its kind
   (`lab.validate_topology_data()`, `validate_access_info_data()`, or the
   minimal "valid YAML mapping" check for scenario/reference); the
   running-config selection's topology/scenario/every reference must exist
   on disk, or be the definition just created/edited in this same commit.
2. On any validation failure: **zero disk writes**, the candidate and
   current mode are retained unchanged, and every failure is printed as a
   `% ...` line.
3. On success, the dirty definition file (if any) is written first, then
   `lab/settings.yaml` if the running-config candidate is dirty — in that
   order, since a running-config selection may reference a definition just
   created in this same commit. Writes are atomic (temp file, flush,
   `os.replace()`); an unchanged definition that was only opened is never
   rewritten, and a fully clean commit ("No changes to commit.") writes
   nothing at all.
4. The CLI returns to EXEC mode on any commit outcome (success or no-op).

## `clear`

Discards **every** uncommitted change in the current configure session —
the running-config candidate and the open definition candidate together —
restoring committed state. It never writes disk and, unlike the original
Step 2 `abort` it replaces, it **does not return to EXEC**:

- A brand-new (never-committed) definition candidate is discarded entirely,
  not reset to an empty version of itself.
- An existing definition's edits are reverted to the last-loaded (committed)
  snapshot.
- If the current submode's target no longer exists after the revert (e.g.
  you were editing a device just added to a topology, and `clear` reverted
  the topology to a state before that device existed, or you were inside a
  brand-new topology/scenario/reference/access-info that just got discarded
  entirely), the CLI steps back to the nearest still-valid parent mode
  instead of lingering in a now-invalid submode:

  ```
  network-lab(config-scenario-new_scenario)# clear
  network-lab(config)#
  ```

`clear` is unrelated to Ctrl-C: Ctrl-C only cancels the current, not-yet-
submitted input line and never touches the candidate.

## `exit` / `end`

- In a device submode (topology device / access-info device), `exit`
  returns one level up to the parent definition mode; no dirty guard (still
  the same overall configuration session).
- In a definition mode (topology/access-info/scenario/reference) or
  running-config mode, `exit` returns one level up to global configuration
  mode; no dirty guard.
- In global configuration mode, `exit` and `end` are equivalent: both
  return to EXEC, guarded by `overall_dirty`.
- `end` from any submode jumps directly to EXEC, guarded by `overall_dirty`.
  `end` is never an implicit commit or clear.

## `show running-config` vs. `show configuration`

- `show running-config` — the **committed MCP definition selection**
  (topology/scenario/reference *names*, never a definition's own content),
  currently on disk, unaffected by any candidate. **Identical in every
  mode** — topology mode's `show running-config` never dumps that
  topology's committed YAML; use `show configuration` for the open
  definition's candidate content instead. Format:

  ```
  network-lab# show running-config
  !
   topology
    sample_lab
  !
   scenario
    sample
  !
   reference
    sample
  !
  network-lab#
  ```

- `show configuration` — the **candidate**: the running-config candidate
  while in `running` mode (same `!`-delimited format as above, but reading
  pending selections), otherwise whichever definition candidate (if any) is
  open — a topology block (safe fields only), an access-info block
  (password-masked), or the raw candidate YAML for scenario/reference (no
  fixed schema yet). Available in every mode except EXEC (there is no
  candidate in EXEC).

Both mask device passwords as `********` and display every identifier using
its exact stored/candidate case.

## Password safety

A device `password` (entered in access-info device mode) is stored in plain
text in access-info YAML, exactly like Step 1 (this is a lab tool, not a
secret manager). It is never shown by `show configuration`/`show
running-config`, never offered as a Tab/`?` candidate or value, and never
retained in the CLI's in-memory command history — see "Command history" and
"Tab / Ctrl-I completion" above. Topology never contains `password` at all
(rejected by `lab.validate_topology_data()`), and no MCP tool response ever
includes it.

## External YAML editor

`edit` (available in topology/scenario/reference definition mode) opens the
**candidate** — never the committed file — in an external editor:

```
committed definition -> candidate data -> secure temp .yaml file
    -> external editor -> editor exits -> read temp file -> parse -> validate
    -> candidate updated
```

- **Resolution order**: `$VISUAL`, then `$EDITOR` (each split with
  `shlex.split()` since it may embed arguments; `shell=True` is never
  used), then a `vim` fallback. Network Lab MCP only adds `-c "syntax on"
  -c "set filetype=yaml"` when it chose `vim` itself — never when the
  operator explicitly set `$VISUAL`/`$EDITOR` to `vim`.
- **Temp file**: a securely created, uniquely named `.yaml` file (never a
  predictable fixed name), removed after the editor exits — on every path,
  including an error.
- **On a non-zero editor exit**: the candidate and committed file are both
  left untouched; a clear `%` error is printed and the CLI keeps running.
- **On invalid YAML**: same as above, with a safe line/column-only message
  (`% YAML validation failed at line 8, column 4. Candidate was not
  updated.`) — the raw editor content is never dumped into the error.
- **`:wq` in the editor does not commit**: the candidate is only updated in
  memory; `commit` is still required to persist it to the real YAML file.
- **No semantic change, no dirty flag**: opening and saving without
  changing anything does not mark the candidate dirty (the same
  `definition_candidate == definition_original` equality check `clear`/
  `commit` already use).

### Why access-info has no external editor yet

access-info's `password` field needs consistent masking in every rendered
view and must never be offered as a completion candidate; an arbitrary
external editor round trip would show and let you retype the plaintext
value outside of that guarded path. Structured CLI editing keeps password
entry on the one path that already enforces this. This may be revisited
later, but is out of scope for this phase.

## Limitations

- No `no topology <name>` or `discover topology` (both Step 3).
- No link editor; an existing topology's `links` (and any other field the
  CLI does not directly edit) are preserved untouched through a commit.
- Scenario and reference content has a schema that is intentionally not
  fixed yet (see [scenario_format.md](scenario_format.md)) — only "valid
  YAML, root is a mapping" is enforced.
- `lab/principles.yaml` is not edited by the CLI.
- The case-only collision safeguard is implemented for `topology <name>`
  only; `device <name>` (under either topology or access-info) and
  access-info/scenario/reference names have no equivalent safeguard
  (identifiers remain fully case-sensitive, but a near-duplicate by case is
  not flagged).
- access-info has no external-editor support in this phase (see above).
- Device access resolution (used by `terminal_open()`, not this CLI
  directly) requires device IDs to be globally unique across all committed
  access-info definitions, since it is not yet scoped by topology — see
  README.md's
  ["Temporary limitation: global device-ID uniqueness"](../README.md#temporary-limitation-global-device-id-uniqueness).
- Candidate configuration is memory-only: it is not recoverable after an
  abrupt process termination (SIGKILL, crash, host failure). Normal exit
  paths (`exit`, `end`, `clear`, `quit`, Ctrl-D) never lose it unexpectedly.
