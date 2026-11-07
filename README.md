# Lanes

External agent lanes for the Hermes Kanban.

A card whose `assignee` is not a Hermes profile is never executed by the
dispatcher — it stays in `ready` with a `skipped_nonspawnable` event. **Lanes**
is the other half of that contract: it claims those cards, hands them to an
outside harness, watches the run, and closes the card on the board with the
agent's own summary.

The board stays the source of truth. Every action goes through
`hermes_cli.kanban_db`, so `task_events` and `task_runs` read exactly like a
native worker's run, and the dashboard's Kanban tab shows the same state.

```
card (assignee=paseo)
   │  dispatcher: skipped_nonspawnable  → nothing runs
   ▼
lanes tick
   ├─ claim_task(claimer="lanes:<lane>:<host>:<pid>")   → card running
   ├─ comment "[lanes] claim run=N ... agent=pending"
   ├─ adapter.launch(card, brief)                        → agent handle
   ├─ comment "[lanes] launched ... agent=<handle>"
   │
   ├─ next ticks: adapter.status(handle)
   │     running → heartbeat_claim
   │     idle    → two sightings → complete_task(summary)
   │     error   → block_task(kind=capability)
   └─ orphaned claim (>90 s, no agent) → reclaim_task
```

## Install

```bash
tar czf - -C lanes_plugin . | ssh oracle 'tar xzf - -C /tmp'
sudo -u hermes-agent cp -r /tmp/lanes_plugin ~/.hermes/plugins/lanes   # as the Hermes user
rm -rf ~/.hermes/plugins/lanes/**/__pycache__
curl -s http://127.0.0.1:9119/api/dashboard/plugins/rescan
```

The dashboard scans `~/.hermes/plugins/*/dashboard/manifest.json`; a rescan picks
the tab up without restarting anything. The backend ticker starts on the first
request to `/api/plugins/lanes/state`.

## Config

`data/config.json`, written with defaults on first use:

```json
{
  "tick_seconds": 60,
  "auto_dispatch": true,
  "lanes": [
    {
      "id": "paseo-tronic",
      "adapter": "paseo",
      "board": "default",
      "assignee": "paseo",
      "adapter_url": "https://your-paseo-host/mcp/agents",
      "provider": "codex/gpt-6-luna",
      "mode": "auto",
      "max_concurrent": 1,
      "enabled": true
    }
  ]
}
```

* `adapter_url` is the endpoint the adapter talks to (`$PASEO_MCP_URL` also works
  and is used when the field is empty). Nothing private is hard-coded: an
  adapter with no endpoint reports itself unavailable instead of guessing.
* `auto_dispatch: false` makes lanes observe-only (no claim, no launch).
* `assignee` must NOT be a Hermes profile name — otherwise the dispatcher would
  spawn a Hermes worker for the same card and you would have two agents.
* `provider` must be `<provider>/<model-id>` as the adapter's `list_models`
  reports it (three segments when the id is already prefixed).
* `max_concurrent` is per lane; the claim lock is per lane too, so two lanes can
  run different harnesses side by side.

Per-card override: a line `paseo-provider: <provider>/<model>` in the card body
is honoured by the Paseo adapter.

## Adapters

An adapter is six methods (`lanes_core/adapters.py`):

| method | returns |
|---|---|
| `available()` | `(bool, reason)` — is this harness usable right now |
| `workspaces()` | `{local_path: handle}` — where it can work |
| `launch(task, brief, provider, mode)` | `{"handle", "provider", "workspace"}` |
| `status(handle)` | `{"state", "updates", "tokens", "detail", "pending"}` |
| `activity(handle)` | transcript tail for the card summary |
| `cancel(handle)` | bool |

`state` is only `running | idle | error | gone`. Adding a harness means
implementing those and calling `adapters.register(MyAdapter)` — the engine, the
board writes and the dashboard tab are untouched.

**Paseo** is implemented: MCP `tools/call` over HTTPS, bearer token read from
`$PASEO_PASSWORD` or from Bitwarden Secrets Manager's cache in the Hermes home.
No secret ever lives in the config.

**A CLI harness later** (claude, codex, opencode): spawn the CLI in the card's
workspace with the brief, keep the pid as handle, map *alive* → `running`,
*exit 0* → `idle`, *exit != 0* → `error`, and attach the log tail as activity.
The two-sighting rule already covers CLIs that exit before flushing their log.

## HTTP API

Mounted at `/api/plugins/lanes/`, behind the dashboard's session auth.

| route | what |
|---|---|
| `GET /state` | lanes, running jobs, board counts, last tick (`?probe=1` also pings each adapter) |
| `POST /tick` | run one tick now (`?dry_run=1` to plan only) |
| `POST /lanes/{id}/enabled` | `{"enabled": bool}` |
| `POST /config` | `{"auto_dispatch", "tick_seconds", "lane": {...}}` |
| `POST /cards/{id}/cancel` | stop the agent, park the card with the reason |

## Why these rules

* **Claim before launch.** The claim note is written before the adapter is
  called, so a crash is visible as `agent=pending` instead of a card stuck in
  `running`.
* **`idle` is not success.** A provider refusal ends `idle` too, with 0 output
  tokens and no tool call; that closes the card as `blocked`, never `done`.
* **Two sightings before closing.** Paseo's transcript lags its own status: a
  `[Shell] cmd` line lands before its output. Closing on one sighting truncates
  the summary.
* **A recent claim is not an orphan.** Without an agent record, nothing is
  reclaimed under 90 s — otherwise two passes fight over the same card.

## Tests

```bash
python3 run_tests.py        # stdlib only, no Hermes, no network
python3 -m pytest tests/ -q # if pytest is available
```

17 cases: dispatch, cap, disabled lane, dry-run, heartbeat, idle confirmation,
empty-run blocking, error blocking, permission reporting, orphan/stale/pending
claims, and the text helpers.
