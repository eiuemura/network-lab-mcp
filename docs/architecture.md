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

### running-config cardinality

`running-config` answers *which* committed definitions are currently
active -- never their content. Each definition type has a different
cardinality, and the CLI's `show running-config <definition-type>`
views (see [cli_reference.md](cli_reference.md#show-running-config-definition-type))
follow it exactly:

| Definition type | Cardinality | `show running-config <type>` |
|---|---|---|
| `access_info` | zero or one | no `<name>`; zero is a legitimate, non-error state |
| `topology` | exactly one, in a valid running state | no `<name>`; missing/invalid is a broken state (fails) |
| `scenario` | exactly one, in a valid running state | no `<name>`; missing/invalid is a broken state (fails) |
| `references` | ordered list -- zero, one, or many | multi-select: bare = all, in committed order; `<name>` = one *active* member only |

These views are a read-only dereference of committed state, never a
general definition-name browser: `access-info`/`topology`/`scenario`
never take a `<name>` (there is at most one active definition to
dereference), and `reference <name>` only accepts a name that is
currently in committed `active_references` -- a reference that exists on
disk but isn't selected is rejected, exactly like it isn't part of
running-config anywhere else in this project.

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

### Concurrency model (Step 3.3)

The core invariant: **different devices execute concurrently; the same
device's operations are serialized.**

```
R1 ---------------->
R2 ---------------->        different devices: parallel
R3 ---------------->

R1 open/send/read/close      same device: serialized through one lock
```

**MCP boundary.** The public MCP interface stays exactly the seven tools
listed above -- there is no `terminal_send_parallel()` or batch API.
Parallelism is a property of the existing tools' execution, not a new tool.
Investigation of the MCP SDK in use (`mcp` 2.2.0) found that this already
works with no change to `mcp_server.py`'s dispatch model: every incoming
`tools/call` request (other than the connection handshake) is spawned as
its own `anyio` task rather than awaited in place
(`mcp.shared.jsonrpc_dispatcher.JSONRPCDispatcher._dispatch_request()`),
and each `@mcp.tool()` function here is a plain synchronous `def`, which the
framework invokes via `anyio.to_thread.run_sync()` -- offloaded to a real
worker thread, never blocking the event loop other requests share. Two
different-device tool calls issued back-to-back by an MCP client can
therefore already overlap.

**terminal.py serialization.** `terminal.py` keeps no session registry of
its own (tmux remains the sole source of truth), so most operations are
naturally device-isolated -- every tmux command is scoped to one session
name derived from the device. Two real races existed before Step 3.3,
both from check-then-act patterns:

- Same-device `open`/`send`/`read`/`close` could interleave (e.g. two
  concurrent `terminal_open()` calls for the same device could both see no
  existing session and both try to create it, the second failing on
  tmux's own duplicate-session error instead of reusing the first).
- The very first session ever created in the tmux server's lifetime could
  race across two *different* devices' simultaneous first opens (both
  see an empty server and both try to create the shared bootstrap
  session).

The fix is one `threading.Lock` per underlying tmux session name
(`terminal._session_lock()`, a small process-lifetime registry keyed by
the already-validated session name -- never a single lock shared by every
device, which would serialize all devices and defeat the point). Each
public per-session operation (`open_device_terminal`, `send_to_device`,
`read_device`, `close_device_terminal`, and the private Discovery bootstrap
equivalents) holds that one lock for its whole body. The rare
cross-device first-bootstrap race is handled separately, by making
`_ensure_tmux_environment()` tolerate losing that race rather than by a
second lock. `terminal_list()` stays unlocked: it is a read-only query
that tmux itself answers atomically.

### Live read-only human monitoring (Step 3.4 / 3.4a)

```
                        AI / MCP
                           |
                    managed terminal
                           |
                           v
                  network-lab-device-R1
                           |
                           |
                           +------+
                                  |
                                  v
                         monitor terminal R1
                                  ^
                           +------+
                           |
                  network-lab-discovery-R1
                           ^
                           |
                    Discovery engine
```

`monitor terminal <device-id>` (EXEC only) gives a human a live view of
whichever Network Lab MCP terminal session currently has priority for that
device, without ever becoming a second writer to it. It is device-oriented,
not tmux-session-oriented: source priority is

    managed session  >  Discovery session  >  waiting

It is built entirely on `terminal.capture_device_terminal_view()` -- a
small, pure observation function alongside the existing production API,
which tries the normal managed session
(`derive_production_session_name()`) first and only falls back to a
Discovery bootstrap session (`derive_discovery_session_name()`) for the
same device if no managed session currently exists, via one shared
private helper (`_observe_named_session()`) for both -- no second
observation implementation. Both paths use only `_pane_state()`/
`_capture_pane()` (has-session/list-panes/capture-pane equivalents), never
`send-keys`/`new-session`/`kill-session`; observing a Discovery session
never creates one (only `discover_topology()` does that) and never delays
its cleanup. It is deliberately not wrapped in the Step 3.3 per-device
lock: every call it makes is already a plain read, the only "race" it
could have (a session disappearing between its own two tmux calls, in
either namespace) is exactly the WAITING/fallback transition it is
designed to tolerate rather than prevent, and since `monitor terminal`
normally runs in a separate `./run_cli.sh` process with its own empty,
process-local lock registry, taking that lock here could not provide real
cross-process exclusion anyway.

The monitor models exactly three states -- `waiting` (neither session
exists), `active` (the selected session/pane is alive), `ended` (pane
exists but its process exited, via tmux's `remain-on-exit`) -- deliberately
nothing richer (no attempt to infer router/BGP/SSH-auth state from tmux),
plus which session `status` describes (`source`: `"managed"`,
`"discovery"`, or `"none"`). Priority is re-evaluated fresh on every
observation, so a managed session appearing while Discovery is being shown
(or disappearing and falling back to a still-active Discovery session)
needs no monitor restart -- the very next refresh reflects it. Monitor
lifetime is independent of session lifetime by design: it starts in
`waiting` if opened before either session exists, survives disappearance/
`ended` in either namespace without exiting, and automatically resumes
live display once a session (managed or Discovery) exists again; only the
human quitting (`q`/`Q`/Ctrl-C) ends it. The UI itself is a small
`prompt_toolkit` `Application` (full-screen, with its own `refresh_interval`
driving periodic re-observation -- no manual polling thread, no
`termios`/`tty`/`fcntl` of our own), matching the CLI's existing
prompt_toolkit-only terminal handling.

Multiple monitors -- of the same or different devices, from separate CLI
processes -- are fully independent: tmux remains the only session state,
so there is no monitor registry, daemon, or IPC layer to keep in sync.
`monitor terminal <device-id>`'s target eligibility (the committed active
topology's devices, union'd with devices that already have an existing
managed *or* Discovery session) is likewise read fresh each time, so a
device being discovered for the first time -- not yet in any committed
topology -- is still a valid target the moment its Discovery session
exists.

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

### Persistent terminal session logging

Every device session -- production and Discovery's private bootstrap
sessions alike -- is logged via tmux's own `pipe-pane` mechanism
(`tmux pipe-pane -o -t <session> 'cat >> <logfile>'`), started right after
the session is first created (never on reuse, since the pipe stays
attached for the pane's whole lifetime). This is the only logging
mechanism: nothing here re-renders or duplicates pane content into a
second application log, and `terminal_send()`'s payload is never logged
separately from what the pane/log itself already shows. tmux's pane
remains the runtime session source of truth and `terminal_read()` is
completely unchanged -- the log is a separate, write-only, persistent
historical record at `logs/terminal/<device-id>/<session-start>.log`
(`YYYYMMDDTHHMMSS`), which is gitignored. `show logging` (EXEC only, see
below) is the only reader of these files.

### Terminal log deletion (Step B / Step B.1 / Step B.1a)

`delete logging all` / `delete logging <device-id> all` / `delete
logging <device-id> <log-file>` (files only) and `delete logging all
directory` / `delete logging <device-id> directory` (files, then the
now-empty device directory itself) (EXEC only) are the only writers
besides the logging mechanism itself. Every one of them, and every path
through `terminal.py`'s deletion backend, is built on the exact same
enumeration `show logging` reads (`list_logged_device_ids()` /
`list_device_logs()`) -- a symlink (a log file, or a device directory
itself) is excluded, never followed or treated as eligible.

`show logging`'s own command surface briefly conflated two different
views: Step B.1 changed bare `show logging` from its original flat
per-file listing into the per-device eligible-log-count summary. Step
B.1a split these back into two separate commands -- bare `show logging`
is once again the original flat listing (`h_show_logging()` in
cli/main.py, restored verbatim from the Step B implementation), and
`show logging summary` (`h_show_logging_summary()`) is the count table,
reachable as `logging`'s `summary` literal child living alongside its
existing dynamic `<device-id>` argument -- the same "a node combines
fixed literal children with a further dynamic argument" grammar shape
Step B/B.1 already introduced for `delete logging`'s `all`/`directory`,
reused here with no further grammar core changes.

`terminal.DeletionPlan` is the shared unit of work: an immutable,
comparable (`==`) snapshot of exactly which files and which device
directories one operation would touch. Each `build_*_deletion_plan()`
function (`build_file_deletion_plan`, `build_device_all_deletion_plan`,
`build_device_directory_deletion_plan`, `build_global_all_deletion_plan`,
`build_global_directory_deletion_plan`) either raises `TerminalError`
(nothing eligible, or something unsafe) or returns a plan; none of them
ever prompt or read input -- `cli/main.py` owns confirmation and message
text entirely (Step B.1's boundary: interactive `[y/N]` behavior does not
belong inside the logging backend). `apply_deletion_plan()` unlinks the
plan's files, then `rmdir`s its directories (never `shutil.rmtree`/a
recursive delete) -- non-recursive by construction, so an unexpected
directory content can only ever block a plan at build time, never cause
a partial deletion at apply time.

`cli/main.py`'s `_confirm_and_apply()` is the shared confirm-then-verify
flow every destructive handler uses: build the plan once (to compute
counts for the `[y/N]` message), confirm, then **build the plan again**
and compare it to the first one before ever calling
`apply_deletion_plan()`. Since preflighting device directories,
enumerating files, and checking active writers are all cheap, read-only
operations, re-running the exact same builder is sufficient to catch
anything that changed while the operator was deciding -- a new log
file, a session that started logging, or a directory that gained an
unexpected entry -- without a bespoke diff/lock mechanism. A mismatch (or
a re-preflight failure, which propagates as an ordinary `TerminalError`)
aborts with nothing deleted; only an unchanged, still-safe plan is ever
applied. `_read_confirmation_line()`/`_confirm_delete()` use plain
`input()`, never `PromptSession`, so a confirmation answer can never
reach `MaskingHistory`. A destructive command reached through
`execute_input_block()`'s multi-line paste path always fails closed
(`execute_command_line(..., interactive=False)` threads a synthetic
`"_interactive": False` into the handler's `args`) instead of blocking on
stdin or risking the next pasted line being misread as the answer --
every other handler ignores that key entirely.

Active-writer protection is the central safety property, and its
granularity is a documented, deliberate limitation rather than an
oversight: both a production session and a Discovery bootstrap session
attach `pipe-pane` logging to the *same* `logs/terminal/<device-id>/`
directory, keyed only by device name (see above), and that attachment is
never explicitly detached before the session ends. Nothing in this
architecture records, anywhere retrievable after session creation, which
exact log file a live session is piping to -- `_start_session_logging()`
computes that path once and its return value is discarded by both
`open_device_terminal()` and `open_bootstrap_terminal()`. The only
reliable, provable primitive is therefore device-level:
`terminal._device_has_active_session(device_name)` checks whether a
production (`derive_production_session_name()`) or Discovery
(`derive_discovery_session_name()`) session currently exists for that
exact device -- if either does, deletion is rejected for **all** of that
device's stored logs, not a guessed "active" one. Validation sessions
never attach logging (`open_validation_session()` never passes
`log_device_name`), so they are never a protection concern here, and
tmux namespace classification is otherwise irrelevant to this check: a
Discovery session is protected because it writes an eligible log, not
because of its namespace.

`delete logging all` / `all directory` / `<device-id> all` / `<device-id>
directory` all preflight their entire target set (files, and for a
`directory` form, directory contents/writers too) before ever asking for
confirmation -- if any targeted device has an active writer, or (for a
`directory` form) any targeted directory contains anything other than
eligible log files, nothing at all is deleted, including the logs of
devices that are themselves inactive. `build_global_directory_deletion_plan()`
reuses `build_device_directory_deletion_plan()` once per valid device
directory rather than duplicating that per-device safety logic, so the
first unsafe device's own error (already naming that device) blocks the
whole operation. `logs/terminal/` itself is never a removal target, only
its valid, non-symlink direct child device directories are; unrelated
files directly under it are left alone. Deletion never closes a session,
stops `pipe-pane`, or otherwise touches session lifecycle; that remains
entirely the concern of `terminal_close()`/Discovery's own cleanup.

### A third session namespace: Discovery bootstrap

```
Production:  network-lab-device-<device-id>
Validation:  network-lab-validation-<validation-id>
Discovery:   network-lab-discovery-<device-id>
```

Step 3's Discovery bootstrap connectivity reuses the exact same
`_build_transport_command()` (direct SSH and single-hop ProxyJump alike)
and session primitives as production, in this third, structurally
separate namespace, so a Discovery session can never collide with,
appear in, or be closed by any public `terminal_*` tool call -- the public
`terminal_open()` topology-membership restriction is completely
unaffected. The one difference passed to `_build_transport_command()` is
`accept_new_host_keys=True` (`ssh -o StrictHostKeyChecking=accept-new`):
Discovery is an unattended flow with no human to answer an interactive
host-key confirmation prompt, so it avoids that prompt outright for a
genuinely new host key, rather than automating the confirmation. It never
bypasses a *changed*-host-key failure (`StrictHostKeyChecking=no`/
`UserKnownHostsFile=/dev/null` are never used) -- that remains a hard
failure the operator must resolve themselves (e.g. `ssh-keygen -R
<address>`) after independently verifying the new fingerprint really is
the expected device, exactly as OpenSSH's own normal host-key security
model requires.

Because Discovery must work before a topology fully exists yet (its
whole point is to help build one), the bootstrap session is opened
directly against a selected access-info device by logical ID, with no
active-topology membership check at all -- unlike `terminal_open()`, which
requires the device to be in the active topology. This is intentional and
does not weaken `terminal_open()`'s own restriction, which is a completely
separate code path.

### Minimal internal command runner

Discovery's login (`_login()`) and per-command execution (`_run_command()`
in `discovery.py`) poll the pane (`_wait_for_pattern()` in `terminal.py`)
for an IOS XR prompt regex (`RP/\S+/CPU\d+:<hostname>#`, which also
doubles as the hostname source -- see below) or a password prompt, with a
bounded timeout that fails closed (`TerminalError`/`DiscoveryError`, never
a silent guess or infinite wait). This is deliberately minimal: there is
no generic expect library, no IOS XR CLI state machine, and no terminal
automation framework -- just "send one command, wait for the prompt to
come back, slice out the output between the echoed command and the
prompt."

## Step 3: IOS XR + LLDP topology discovery

```
committed active_access_info
    -> discovery._select_iosxr_targets()  (type iosxr only; host/iosxe/
       nxos are skipped, not failed, unless zero iosxr targets remain)
    -> discovery._bootstrap_collect()  (per device: login, `terminal
       length 0`, `show version`, `show running-config`,
       `show lldp neighbors` -- all logged persistently, see above)
    -> discovery.parse_lldp_neighbors()  (raw text -> LldpObservation,
       never resolving identity itself)
    -> discovery.resolve_remote_identity()  (LldpObservation.remote_
       device_id_raw -> logical device ID, or None -- fails closed on
       anything ambiguous)
    -> discovery.reconcile_links()  (resolved observations -> ManagedLink
       list + LinkConflict list)
    -> discovery.DiscoveryResult  (in-memory only)
    -> cli/config.py CliSession.apply_discovery_result()  (opens/creates
       the topology candidate via the *same* plan_topology_definition()/
       apply_topology_definition_plan() as a manually typed `topology
       <name>`, then merges in discovery.build_topology_devices_and_links())
```

`discovery.py` owns every Discovery-specific behavior (bootstrap
connectivity reuses `terminal.py`'s primitives, but the login sequence,
command runner, parser, identity resolution, and reconciliation are all
here) and is the *only* new module Step 3 adds; `cli/config.py`'s
`apply_discovery_result()` is the only new candidate-mutation logic, and
it is a thin adapter onto the pre-existing topology candidate machinery
-- there is no separate Discovery datastore, history table, or schema.

### Parallel per-device collection (Step 3.3)

`_bootstrap_collect()` for each device now runs on a bounded
`concurrent.futures.ThreadPoolExecutor` (`DISCOVERY_MAX_WORKERS = 8` --
small, internal, not a CLI/config knob; comfortably covers real lab scale
while bounding concurrent SSH/tmux session creation) instead of a plain
sequential loop:

```
R1 collect -----|
R2 collect -----|
R3 collect -----| concurrent, bounded by DISCOVERY_MAX_WORKERS
R4 collect -----|
                |
                v
     every future finished (success or failure)
                |
                v
     close every target's bootstrap session (unconditional cleanup,
     unchanged ownership: one outer step closes all of them, exactly
     as the previous sequential loop's own try/finally did)
                |
                v
     aggregate results in original target order, never completion
     order -- parsing/identity/reconciliation are unchanged and still
     run only after every device's result (or first failure) is known
```

Parallelism is device-level only: within one device, `_bootstrap_collect()`
itself is untouched (login, then `terminal length 0`, `show version`,
`show running-config`, `show lldp neighbors`, strictly in that order, in
that device's own worker). Workers are value-oriented -- each returns its
own collected dict; nothing here mutates a shared candidate, link list, or
conflict list from more than one thread. Aggregation and error attribution
are always by original target order, not by whichever thread happened to
finish first, so scheduler order can never change which device's result
lands where or which device's failure is the one reported. Any collection
failure -- expected (`DiscoveryError`) or an unexpected worker exception,
which is still converted to a bounded `DiscoveryError` rather than leaking
a raw traceback -- still fails the whole operation with zero candidate
mutation, exactly like the pre-Step-3.3 sequential loop.

### Identity resolution is bounded and fails closed

`resolve_remote_identity()` matches an LLDP remote Device ID against an
in-memory `{logical_device_id: observed_hostname}` map built from each
bootstrap session's own IOS XR prompt, in this order, each step requiring
a *unique* match or the neighbor stays unresolved:

1. exact observed-hostname match
2. case-normalized exact match
3. short-name/FQDN-style alias match (the raw ID's segment before its
   first `.`, compared case-insensitively against each hostname --
   e.g. `APJC_JP_OSK_R2.cisco` matches hostname `APJC_JP_OSK_R2`)

There is no substring search, no fuzzy matching, and no inference from
the logical device ID itself (never `"R2" in device_id`). A neighbor that
resolves to more than one logical device at any step is treated exactly
like one that resolves to none -- unresolved, never guessed.

### Managed vs. unresolved neighbors, and where their evidence lives

A resolved neighbor that is also one of *this run's* selected IOS XR
targets becomes a candidate topology device/link. Everything else --
external routers, LLDP-visible but not in the selected access-info, or
genuinely ambiguous -- is an **unresolved neighbor**: never invented as a
managed topology device, but not silently dropped either. Its full raw
evidence (remote Device ID, observing device/interface, remote port,
capability) is:

- rendered directly in that `discover topology` run's own CLI output
  (mandatory, not reducible to just a count), and
- separately, persistently recoverable afterwards from the observing
  device's own terminal log (`show logging <device-id> <log-file>`,
  since the original `show lldp neighbors` output is right there).

Deliberately, there is **no third persistence layer** for this evidence:
no `show discover`/discovery-history command, and no unresolved-neighbor
record written into the committed topology YAML. Re-running `discover
topology` produces a fresh normalized result the same way every time.

### Link reconciliation

`reconcile_links()` indexes resolved observations by their own
`(local_device_id, local_interface)` key (never by a "canonical pair"
derived from a claimed remote port, since the two sides of a conflict may
claim *different* remote ports for the same local interface -- keying by
each side's own dict entry is what correctly prevents double-processing
in that case). For each local endpoint not yet consumed:

- if the *remote* endpoint has no observation of its own, the link is
  one-sided but still created (section 48: bidirectional LLDP is not an
  absolute requirement);
- if it does, and it reciprocally agrees (claims the same original local
  device/interface back), the pair collapses into one `ManagedLink`;
- if it does, but disagrees (a different interface mapping), a
  `LinkConflict` is recorded instead -- the link is never silently
  created from either side's guess.

A link's identity is its unordered pair of `(device, interface)`
endpoints, so two parallel links between the same router pair on
different interfaces are never deduplicated together.

### Topology candidate merge is conservative

`build_topology_devices_and_links()` only ever *adds* to whatever
devices/links already exist in the target topology's candidate (new or
already-committed): existing devices/fields/links are never removed, and
a discovered link already present (compared by its endpoint-pair key,
independent of field order) is never duplicated. This is why Discovery
never needs a special "diff" of its own beyond the ordinary candidate
system already documented above (candidate/original/dirty tracking,
`show`/`show configuration`/`show running-config`/`commit`/`clear`/
`root`/`exit`/`end`) -- `discover topology` is, structurally, just another
way to populate a topology candidate, the same way the external YAML
editor is.

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

A nested submode (a topology/access-info device, or an access-info jump
host) is only a context pointer (`current_device_name` /
`current_jump_host_name`) into that one owning definition candidate — it
never becomes, or is backed by, a second independent candidate. Entering
`device R4` from an existing access-info definition adds `R4` to the
*same* `definition_candidate["devices"]` dict that already holds every
other committed device; `commit` issued from inside `R4`'s own submode
persists that whole dict, siblings included, never a scoped fragment.
`show`/`show configuration` inside a submode *is* scoped to just that one
object (see cli_reference.md), but that is a rendering choice only — it
must never be read as "this is the only object in the candidate," and it
is never commit's source of truth (commit reads `definition_candidate`
directly, never a renderer's output).

`show configuration`'s delta for access-info devices/jump-hosts is
computed against both the committed and candidate maps: an object
present in committed but absent from the candidate (removed via `no
device <name>` / `no jump-host <name>`) renders as a single ` no device
<name>` / ` no jump-host <name>` line, not just silently absent — see
cli_reference.md's "Uncommitted-changes-only `show configuration`" for
the full rendering rules, including the access-info-only standalone `!`
paste round-trip behavior (cli_reference.md's "Multi-line configuration
paste").

### Symmetric definition deletion (Step C topology, generalized in Step D)

`no <kind> <name>` (global configuration only; `<kind>` is
`access-info`/`topology`/`scenario`/`reference`) extends the single
`definition_candidate` slot above with a prospective-absence
representation, rather than adding a second, independent
"deletion set" data structure: `definition_kind`/`definition_name`/
`definition_original` stay set to the real committed definition being
removed, while `definition_candidate` itself becomes `None`
(`CliSession.remove_definition(kind, name)`, added for topology in Step
C and generalized to every kind in Step D behind the same one method --
`remove_topology_definition()` is now a one-line wrapper kept for its
existing callers/tests). `definition_dirty()` treats this as always
dirty (a real committed original always differs from eventual absence).
Since all four kinds already shared this one candidate slot before Step
C ever existed, this was already a stronger invariant than "one topology
candidate" -- at most one dirty *definition of any kind* per configure
session -- and deletion of any kind participates in exactly that same
existing rule with no widening.

This minimal representation change means every existing mechanism
already does the right thing with no further changes:

- `_enter_definition()`'s existing reload guard only skips reloading when
  `definition_candidate is not None` -- so re-entering the *same*
  topology (`topology <name>`) while it is pending deletion naturally
  fails that guard and reloads fresh from disk, restoring the original
  candidate and cancelling the deletion, for free.
- `clear()`'s existing "restore from `definition_original`" branch
  (`self.definition_candidate = copy.deepcopy(self.definition_original)`)
  cancels a pending deletion the same way, with no special-casing.
- `can_switch_definition()` needed no changes at all: since deletion
  makes `definition_dirty()` True, attempting to edit or delete a
  *different* definition of any kind while a deletion is pending is
  already rejected by the pre-existing guard, and the existing "same
  identity is always allowed" check
  (`(self.definition_kind, self.definition_name) == (kind, name)`)
  already permits every same-definition transition (edit -> delete,
  delete -> restore, delete -> restore -> edit) for every kind.
- Deleting a definition that was only ever a brand-new, never-committed
  candidate (`definition_original is None`) is instead a net-zero
  cancellation: the whole candidate slot is discarded outright (the same
  case `clear()` already handles for a never-committed definition),
  since there is nothing real to mark absent.

`_render_configuration_delta()` (cli/main.py) is the one place that
needed an explicit new branch: a `None` candidate alongside a non-`None`
`original` renders as a single `no <kind> <name>` line (never the
per-field diff the other renderers produce -- for access-info,
deliberately never re-rendering that definition's own stored
credentials), and `commit()` (cli/config.py) needed a parallel deletion
path per kind: skip the normal per-kind validator (there is nothing to
validate), reject the commit if the definition being deleted is still
*effectively* (candidate, not already-committed) referenced by
running-config -- `active_access_info` (optional), `active_topology`
(mandatory), `active_scenario` (mandatory), or present in
`active_references` (a list) -- so a combined commit that also switches
the active selection to something else in the same transaction is
correctly allowed, generalized behind one small `_existence_error()`
closure rather than four hand-written checks. Deletion itself dispatches
through `_DEFINITION_DELETERS[kind]` (`lab.delete_topology` /
`delete_access_info` / `delete_scenario` / `delete_reference`), each
requiring an exact match against that kind's own `list_*_names()` and a
non-symlink, path-confined regular file
(`lab._stored_definition_is_deletable()`, one small shared primitive
behind four still-distinct named functions -- not a generic definition
framework), mirroring the discipline already used for terminal log
deletion (`terminal.py`). No cascade: no other definition, running-config
field, terminal session, or Discovery state is ever touched by a
definition deletion.

Investigation for this task found no real cross-definition reference to
an access-info, scenario, or reference definition by name anywhere else
in the schema either (scenario/reference are open-ended YAML documents
with no fixed cross-reference fields; access-info is keyed by device
name, never by another definition's name) -- the only real references
anywhere are running-config's own four selection fields, handled above.
It also confirmed `config-running` mode has no `no topology` command at
all (only `no access-info` and `no reference <name>`), since
`active_topology` (like `active_scenario`) is a mandatory field with no
safe "unset" value -- so there is no actual naming collision with the
global-configuration `no <kind> <name>` commands to resolve, only a
documentation clarification (see cli_reference.md). Separately, Step D
found that `config-running# topology <name>` (and `access-info`/
`scenario`) used a plain, non-enumerating identifier argument -- Tab
completion already worked (the same provider `topology <name>` editing
uses), but bare `?` only ever showed a generic `<name>` placeholder
instead of the actual selectable names. Fixed by giving each
running-config selector its own `enumerate_when_empty` provider
(`provide_running_*_names()` / `CliContext.running_*_names`) reading a
*separate*, candidate-aware field from the one global editing uses --
one that excludes a definition currently pending deletion in the
definition-editing candidate scope, since selecting it here would let
running-config reference a definition about to become absent. Global
`topology <name>` editing deliberately keeps reading the unfiltered
`topology_names` field instead, since offering the pending-deleted name
back there is exactly how its own deletion gets cancelled.

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

- CDP discovery, IOS XE/NX-OS discovery, SNMP/NETCONF/RESTCONF discovery,
  and a generic discovery/plugin framework -- Step 3 implements only IOS
  XR + LLDP (see "Step 3: IOS XR + LLDP topology discovery" below).
- Automatic stale topology-link pruning, `discover topology` automatically
  committing or selecting `active_topology`, and a discovery-history
  subsystem (`show discover`/a discovery database) -- none of these are
  in scope even for the implemented Step 3.
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
