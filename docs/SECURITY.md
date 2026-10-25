# Security model

Wednesday is a local agent with real access to your machine. The model is treated as **untrusted**: it can propose actions, but only code in `agent/permissions.py` decides what happens.

## Boundaries

| Boundary | Mechanism | Tested by |
|---|---|---|
| Network exposure | API bound to `127.0.0.1`; the model server bound to `127.0.0.1` with a random `--api-key` per launch | `test_backend.py` |
| Other local programs | Bearer token in `~/.local/state/wednesday/token` (0600, new every start) | `test_requires_token` |
| Malicious web pages (CSRF / DNS rebinding) | Requests with any `Origin` header → 403; `Host` must be `127.0.0.1:port` / `localhost:port` | `test_rejects_browser_origin`, `test_rejects_foreign_host_header` |
| Model → filesystem | `realpath` resolution (defeats `..` and symlink escapes), read roots, write roots, deny paths, and deny name patterns (`.env`, `*.pem`, `id_rsa*`, `*token*`, …) | `test_permissions.py` |
| Model → shell | No shell is ever spawned. Commands are tokenised, then `; && \|\| > < & $( ) \`` are refused. Pipes are allowed, but **each stage is classified**. Blocked list → refuse; read-only list plus read-only sub-commands → run; everything else → ask you | `test_shell_chaining_refused`, `test_pipe_each_stage_checked`, … |
| Model → network (SSRF) | Only public http(s); a resolver re-checks every IP **at connect time** (DNS-rebinding safe); NAT64 addresses are judged by their embedded IPv4 | `test_url_checks`, `test_public_ip_classification` |
| Model → its own rules | Config dirs, the source tree, and the state dir are on the deny list for reads *and* writes | `deny_paths` in `permissions.yaml` |
| Prompt injection from the web | Web text is fenced as `UNTRUSTED WEB CONTENT`, and the prompt says it carries no authority. More importantly, nothing it says can bypass the permission layer | design |
| Secrets in child processes | Environment variables named `*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, … are stripped before any command runs | `shell.clean_env` |
| Runaway processes | Each command runs in its own process group with a timeout (max 300 s) and output caps; **emergency stop** kills all of Wednesday's commands (never anyone else's) | `test_emergency_stop` |
| Approvals | Per action, never remembered; scoped to the requesting job (a terminal client can't approve the panel's request); timeout = deny; no input = deny | `test_write_needs_approval_and_deny_works` |

## Privacy

- **No cloud model, no fallback, no telemetry.** llama-server runs with `--offline`, and HF downloads set `HF_HUB_DISABLE_TELEMETRY=1`.
- **Online actions** (web search, page fetch) show 🌐 in the panel, and the ONLINE/OFFLINE switch disables them entirely. Verified: with all networking removed (`unshare -rn`), chat works and web tools fail with an honest message.
- **Web requests** identify themselves honestly (`Wednesday/0.1 … aiohttp/x`). There is no browser impersonation and no CAPTCHA/bot-check bypass. Contact info in the UA is opt-in (`web.contact`).
- **Logs** never contain prompts or replies unless you set `logging.log_content: true`.
- **Voice:** audio is recorded only while the mic button or keybind is active, auto-stops after 60 s, is stored in a 0700 directory, and is deleted immediately after transcription.
- **Memory** stores only what you explicitly ask her to remember, plus short compression summaries (toggle: `memory.save_summaries`). You can inspect and delete from the Memory tab, or clear everything via `DELETE /api/memory/all`.

## Known gaps (honest list)

- Read-only commands such as `cat` or `grep` can read anything inside `read_roots` that isn't denied. That covers your whole home directory by default. Narrow `read_roots` if that's too broad.
- An approved `python3 script.py` runs with your full user privileges. Approval is the control here: read the command before you click.
- There is no OS-level sandbox (bubblewrap/landlock) around commands yet. The permission layer is the only barrier. bubblewrap would be the next step.
- The API token protects against other users and web pages. It does **not** protect against malware already running as your user, which could read the token file. Nothing at user level can.
