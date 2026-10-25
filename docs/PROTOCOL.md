# Local API & event protocol

Base URL: `http://127.0.0.1:7377`.

Every route except `/api/health` requires `Authorization: Bearer $(cat ~/.local/state/wednesday/token)`. Requests carrying an `Origin` header are rejected.

```bash
T=$(cat ~/.local/state/wednesday/token)
curl -s -H "Authorization: Bearer $T" localhost:7377/api/status | jq
curl -s -H "Authorization: Bearer $T" -X POST localhost:7377/api/chat -d '{"text":"hi"}'
```

## REST

| Method & path | Body | Result |
|---|---|---|
| GET `/api/health` | – | `{ok}` (no auth) |
| GET `/api/status` | – | state, mood, model, settings, voice, memory, resources |
| GET `/api/diagnostics` | – | status + Resource Guardian protections + internet reachability |
| POST `/api/chat` | `{text, session?="desktop", think?: auto\|fast\|deep, source?}` | `{job}`; output arrives as events |
| POST `/api/cancel` | `{job?}` | cancel the current generation |
| POST `/api/stop` | – | **emergency stop**: cancel, drop queue, deny approvals, kill tool processes, stop speech |
| POST `/api/approve` | `{id, approved}` | answer an approval request |
| POST `/api/reset` | `{session}` | forget a conversation's short-term history |
| GET `/api/history?session=` | – | last 60 user/assistant messages |
| POST `/api/settings` | `{online?, speak?, thinking?, memory?}` | live settings |
| GET `/api/memory?q=` · POST `/api/memory` `{content, kind, project, pinned}` · DELETE `/api/memory/{id\|all}` | | memory CRUD |
| GET `/api/tasks` · DELETE `/api/tasks/{id}` | | task plans |
| POST `/api/model/load` · `/api/model/unload` | | model lifecycle |
| POST `/api/voice/ptt` | `{action: toggle\|start\|stop\|cancel}` | push-to-talk |
| POST `/api/voice/say` `{text}` · `/api/voice/stop` | | speech output |
| POST `/api/ui` | `{command: toggle_avatar\|toggle_panel\|show_panel\|ghost\|quit_ui}` | relayed to the UI as a `ui` event |
| POST `/api/shutdown` | – | graceful backend shutdown |

## Events (`GET /api/events`, WebSocket)

Every event is JSON with `type`, `seq`, and `ts`. On connect, the server sends `hello` (a full status snapshot) and then replays the last `state` / `settings` events.

| type | payload | avatar / UI reaction |
|---|---|---|
| `state` | `state` ∈ IDLE LISTENING THINKING SPEAKING WORKING SUCCESS ERROR SLEEPING, `mood`, `detail` | drives the state machine and expression |
| `job_queued` / `job_started` | `job, session, text?, source` | new bubble |
| `thinking` | `job, text` | reasoning stream (collapsible) |
| `token` | `job, text` | reply stream; mouth pulse when not speaking aloud |
| `retract` | `job` | clear the partial reply (a `</think>` leak was detected) |
| `tool_start` / `tool_end` | `job, tool, summary, ok, preview` | ⚙ lines |
| `tool_blocked` | `tool, summary, reason` | ⛔ line |
| `approval_request` | `id, job, session, tool, summary, reason, destructive, details, quip` | approval card (the panel opens itself) |
| `approval_resolved` | `id, approved` | |
| `reply_done` | `job, text, mood, elapsed_s, stats, thinking, used_tools` | |
| `error` / `cancelled` | `job, message, quip` | ERROR state + bubble |
| `speech` | `active, envelope[], frame_ms, duration, text` | lip-sync from the real audio envelope |
| `listening` | `active, max_seconds` | mic indicator |
| `transcript` | `text` | |
| `quip` | `text` | speech bubble |
| `model` | model status | status chip |
| `resources` | Resource Guardian snapshot | meters |
| `memory_saved`, `task`, `network`, `warning`, `settings`, `ui`, `emergency_stop`, `shutdown` | | |

The client may send `{"type":"ui_metrics","fps":12.3,"pid":1234}`; the Resource Guardian reports it.

**Writing a different avatar** (Live2D, a 3D model, an e-ink badge…) means implementing a WebSocket client for these events. The backend doesn't change.
