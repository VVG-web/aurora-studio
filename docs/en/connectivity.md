# Connection availability and stability check

Part of the kit's work depends on the connection: sources live behind a VPN, the web is on the internet, models are at
providers, and some work needs no network at all. The kit used to know nothing about it: a broken connection looked like a
«source timeout» or «provider did not answer», the engine cycled requests through the ring of gateways and waited blindly. Now
the connection is described explicitly and checked before the work, not after its traces. Русская версия:
[../connectivity.md](../connectivity.md).

The check is **off until the owner turns it on**: without a setup the kit works exactly as before, checks nothing and blocks
nothing. Everything is set up in the panel — there is no need to edit files by hand.

## Where to start

Panel → **Connection check** (the «Machine» group). While there are no profiles, the section explains what a probe is and offers
a three-step wizard:

1. **Name the profile**, for example `VPN-1`. The name is for you to recognise it: sources and model providers refer to it
   later, and a project holds no addresses at all.
2. **Enter an address that answers only while the VPN is on** (an internal service, not a public site), choose the type of check
   and, for TCP, the port.
3. **Press «Check»** — the panel shows the answer, the time and what counts as normal. If the VPN is off right now there will be
   no answer, and that is fine: the profile can be added and checked later.

Then turn on «Connection check is on» and save. Each field has an explanation and an example (`example.com`); «Check now» works
before saving (the «Check» button of the probe itself) and after.

## Profiles and probes

- A **profile** is the name of a communication channel (`VPN-1`). It checks nothing by itself.
- A **probe** is an address that answers only when the connection is there: for example, an internal service behind a VPN, or
  any site you trust to check the internet. A profile may have several probes. **No more than ten for the whole kit.**

| Type | Good for | Notes |
|---|---|---|
| **TCP** | «the port is open» | host and port, for example `example.com`, 443 |
| **HTTP/HTTPS** | «the service answers» | the status (2xx and 3xx by default; you may give `200`, `2xx`, `401`) and an optional text at the start of the answer; redirects are not followed; no login and password in the address |
| **ICMP (ping)** | «the host answers» | through the system `ping`; many servers and networks drop ICMP — then use TCP or HTTP |

The answer time and the frequency are set per probe or once for all (the «Common settings»). The addresses are kept on this
machine and never go into the project's git.

## The profile rule and anti-flapping

A profile gets its state from the probes' answers by a rule:

- **`any_healthy`** — available while at least one address answers (recommended: one failed service does not take the whole VPN
  down);
- **`quorum(K)`** — available if at least K addresses answer;
- **`all_healthy`** — available only if all answer.

A profile does not change state at once: it becomes **unavailable** after N checks in a row failed, and **available** again after
M checks in a row succeeded (3 and 2 by default; set for all or per profile). Between checks the state holds, and blinking is
not shown. A series of checks within a second or two (several presses of «Check now») counts as one assessment. The first check
after a long break (data older than three intervals) decides the state at once — there is nothing to compare with.

## States

A profile: `online` — available; `degraded` — the rule is met but not all probes answer; `offline` — the rule is not met;
`unknown` — not checked yet, or the check is older than three intervals.

The kit as a whole is judged **only by what the work needs**: by the enabled sources of the selected project and by the
providers from the «Models» section, not by all the profiles described.

| State | Meaning |
|---|---|
| `online` | everything needed is available |
| `degraded` | everything needed is available, but some probes do not answer |
| `partial` | part of what is needed is unavailable: features that do not need it work |
| `no_vpn` | the internet is up, but all the profiles needed are down |
| `offline` | no internet: only features that need no connection are available |
| `unknown` | the state is not determined |

The **internet** is the built-in profile `internet`. Its addresses are set by the owner alone: **the kit never goes to third-party
addresses on its own**. While there are no addresses, the internet is «unknown» — which is not «no internet», and nothing is
blocked. Enter any address you trust (for example, `example.com`).

## Where it shows

- **The indicator in the panel header** — the kit's state (it appears when the check is on); a click opens the section.
- **The «Connection check» section** — the mode of the kit, the profiles, the probes with the last answer and its time, the
  internet, the journal of state changes. The background check runs in the panel while it works.
- **`kit:doctor`** — a «связь: …» line from the saved state.
- Messages: with no internet — «Mode of work — offline, only features that need no connection are available»; with an
  unavailable profile — «VPN-1 is down; features that do not need VPN-1 and web features are available».

## Sources: the access mode

Panel → **Project settings → Source access mode**. For every connected source you say how it is reached:

| Mode | Meaning |
|---|---|
| no VPN, needs the internet | web pages and the like: no profile is needed, but without the internet the source is unavailable |
| needs a VPN profile | a source behind a VPN; you choose a profile from the «Connection check» section |
| local or cached | read from this computer, no network needed |
| mixed: VPN and a local cache | fresh data comes through the profile, what was read stays available from the cache |

Two flags are independent of the mode: **«Needs the internet»** (needed regardless of the VPN: «no VPN» does not mean «no
internet») and **«Available from the cache without a connection»**. The project keeps only the **name** of the profile (the
`source_access:` block in `aurora.config.yaml`; the save leaves the rest of the config alone); the project holds no check
addresses.

Defaults: the web module is «no VPN, needs the internet» (its manifest says so, `access.mode`); other sources and providers
with no binding to a profile **count as available, as before**: until the owner sets anything up, the behaviour does not change.

## Model providers

Panel → **Models** → for a provider: **«Works through the profile»** (a profile from «Connection check» or «no VPN») and
**«Provider endpoint check»**: «do not check» (default), «does it answer» or «answers with the key». The availability of a VPN is
not the availability of a provider: the VPN is up and the provider is down — the provider still counts as unavailable. With the
«with the key» check the key goes only to the provider's own address and only over a protected channel (https, or this
computer). Old model setup files are read unchanged, a provider has no binding. A provider on this very computer (`127.0.0.1`)
depends neither on a VPN nor on the internet; a cloud provider without a VPN depends on the internet.

## Command dependencies

Every command has dependency metadata (`command_deps.json`): which sources are needed, whether the kit's model is needed,
whether the internet is needed, whether it can work without a connection. The defaults come from the command registry: `sync:*`
depends on the source of its module, a command with a model depends on the LLM, a command with no sources and no model works
without a connection too; single commands are edited in the file. The profiles without which a command is unavailable in a
project are **computed** from the access modes of the sources and the providers' bindings.

The **«Commands»** tab shows for each command: a type label (1 — depends on sources or the network, 2 — on the model, 3 — on
neither; the labels are for display only), the VPN profiles, whether the model is needed, whether it works offline and which
sources it uses.

## What happens when the connection is not enough

With the check on and something the work needs unavailable, the function **does not start** (instead of failing by timeout minutes
later), and the reason is named: "VPN-1 is unavailable (needed by source "Wiki")", "the internet is unavailable", "all model
providers are unavailable — VPN-1 is not answering". Only a **known** "unavailable" blocks: `unknown` (no checks yet, or the
internet has no addresses) stops nothing. Commands that need no connection (`kb:lint`, `kb:repair`, `net:status`…) always run.

| Where it starts from | What happens |
|---|---|
| A command button in the panel | a dialog with the reason and a choice: **queue** (the job waits for restoration with no limit and starts by itself; "Stop" cancels the wait) or **cancel** |
| A route, the "Run" button | a dialog listing the steps that lack the connection: **only what is available**, **queue**, or **cancel** |
| Cron, bots on a schedule | the policy (below): the common one from the "Connection check" section, or the task's own |
| A source sync from the command line | the script does not start: the reason, exit code 2 and a machine line `AURORA-NET {…}` by which routes and the schedule recognise a connection refusal |
| A model call | an unavailable provider is not touched and is not quarantined; all unavailable — a refusal with the reason (one-off call) or a wait (route step) |

### Policies for automatic runs

| Policy | A step that lacks the connection |
|---|---|
| `fail_fast` | the run does not start (or the step refuses): the reason is in the result, a "no connection" code |
| `partial` | the step is skipped with a note, the rest goes on; the result is "partial" and names what was skipped |
| `queue` | the run waits for restoration with no limit and goes whole |
| `partial_queue` (default) | what is available goes now, the rest is deferred and repeated at the end — after restoration (it waits there) |

A "cycle:" block does not spin idle: a step without a connection stops the lap, and the block repeats whole after restoration
(`partial_queue`) or goes on without that step (`partial`). Waiting has no time limit anywhere — a route sees the work through
until a person stops it; "Check now" wakes the wait. The step's process learns the policy too (`AURORA_NET_POLICY`): a model call
inside it either waits for the providers to return (`queue`, `partial_queue`) or refuses at once (`fail_fast`, `partial`).

### What became of the old waiting

With the check off everything is as before. With it on, each "wait blindly" is replaced by waiting on a known reason, and when the
check does not know the reason the old behaviour stays:

| Before | Now (check on) |
|---|---|
| Agent run after a drop: a 5-minute pause, up to 24 times (`OFFLINE_WAIT`, `OFFLINE_TRIES`) | all models unavailable by the check — wait for them to return, re-checking every 15 seconds; reason unknown — the old pause |
| A route step fell over the network: 15 minutes (30 seconds if the step did work), "Check now" wakes it | reason known — wait for restoration by the check and repeat at once; unknown — the old rhythm |
| Provider quarantine in the ring: 15 minutes after a drop, "Return to the main one" | the drop is explained by an unavailable profile or the internet — no quarantine, the provider returns with the connection; a real provider failure — quarantine as before |
| A request to a provider whose profile is down went out and failed by timeout | no request: the provider is skipped until the profile returns |
| The step watchdog asked the model and the source servers with direct requests | takes the connection check's summary for what the work needs; servers whose access mode is not set are asked directly, as before |
| A lost connection → a direct HTTP request bypassing the adapter | not done: the direct-request reserve is only for a broken adapter, not for a lost connection |
| Cron: a failed step was deferred and repeated at the end | a step without a connection is deferred by the same mechanism (`partial_queue`), the repeat at the end waits for restoration |

## Commands

Read and check only — the settings are written by the panel:

- `net:status` — the state of profiles, probes, the internet and providers, the mode of work and the reasons. The probe addresses
  are not printed (`--targets` prints them); `--json` is machine-readable.
- `net:probe` — check now: all probes or `--only` (a profile or a probe). Exit code 1 when the profiles in need are down. With
  the check off it does nothing.

## What is stored where

- Profiles and probes — in the kit, `local/connectivity.json` (on the machine, never goes to git, no secrets). It is a storage
  format, not a way of setting up: it is edited in the panel, the write is protected against a lost update (a page opened before
  someone else's edit gets a refusal and does not overwrite it), and unknown fields of the file survive a save.
- The state of the checks and the journal of changes — `~/.aurora/connectivity-state.json`: it is written by the panel's
  background check and by any process that checked the connection before a run.
- The data in the state is fresh for one interval; stale data is re-checked on the spot before a run (all probes at once, within
  three seconds). Data older than three intervals counts as unknown.

## Appendix: storage format

An example of `local/connectivity.json` (the addresses and names are made up):

```json
{
  "version": 1,
  "enabled": true,
  "settings": {"interval": 60, "timeout": 3, "fail_after": 3, "ok_after": 2},
  "policy": {"auto": "partial_queue"},
  "profiles": [{"name": "VPN-1", "aggregate": "quorum", "quorum": 2}],
  "internet": {"aggregate": "any_healthy"},
  "probes": [
    {"id": "p1", "profile": "VPN-1", "type": "tcp", "host": "service-a.example.com", "port": 443},
    {"id": "p2", "profile": "VPN-1", "type": "http", "url": "https://service-b.example.com/health", "expect_status": ["2xx"]},
    {"id": "p3", "profile": "internet", "type": "tcp", "host": "example.com", "port": 443}
  ]
}
```
