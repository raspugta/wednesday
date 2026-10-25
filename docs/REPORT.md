# Wednesday: build report (2026-10-24)

Everything below was **run on this laptop**: HP Victus, i5-13420H, 15 GB RAM, RTX 3050 4 GB Laptop GPU, Ubuntu 26.04.1, Hyprland 0.53.3. Where something is not verified, it says so.

## Run it

```bash
wednesday          # from anywhere (or ~/Wednesday/wednesday)
```

## Status by requirement

| Area | Status | Evidence |
|---|---|---|
| Local LLM (no cloud) | ✅ | Qwen3.5-2B Q4_K_M on llama.cpp Vulkan, RTX 3050; `--offline`; no fallback path exists |
| Works with internet removed | ✅ | Backend run in `unshare -rn` (loopback only): chat answered in 1.46 s; web search failed with an honest "cannot reach the internet" |
| Desktop avatar (8 states, 11 moods) | ✅ | Layer-shell OVERLAY surface `wednesday-avatar`; screenshots of every state |
| Transparency | ✅ | Verified visually over other windows |
| Click-through | ✅ | Cursor on a transparent pixel *inside* the surface → no pointer-enter; on her face → enter. Ghost mode → no enter anywhere |
| Never steals focus | ✅ | `hyprctl activewindow` unchanged after avatar start, hide/show, and panel open |
| States driven by real events | ✅ | THINKING / SPEAKING / WORKING / SLEEPING observed during real requests; event protocol in PROTOCOL.md |
| Hide/restore, move, resize | ✅ hide/show via CLI (layer verified gone/back) · ⚠ drag & Ctrl+scroll are implemented but **not exercised by me**: there's no pointer-injection tool here | see "Please verify" |
| Chat panel | ✅ | Opens beside her, streams markdown, approval cards, tasks/memory/system tabs, live meters |
| Tools + permissions | ✅ | 53 automated tests, plus live checks: `~/.ssh/id_rsa` BLOCKED; `git status` ran; write → approval → deny left the file unwritten |
| Destructive commands | ✅ | `sudo`, `dd`, `mkfs`, `rm` outside write roots, chaining → BLOCKED (tests) |
| Memory | ✅ | SQLite FTS5; explicit "remember" saved, then recalled in a new session; integrity OK after `kill -9` |
| Web research | ⚠ partial | Page fetching works (GitHub releases page). **Search is unreliable without a key**: DuckDuckGo returns 202 to automated clients and Wikipedia wants contact info. See limitations |
| Voice | ✅ | Ordinary speech transcribed exactly (3/3, about 1.2 s each); a real push-to-talk "How are you?" from you was transcribed correctly; TTS plays with lip-sync |
| Resource Guardian | ✅ | Admission checks use the driver-reported GPU slowdown temperature and coretemp "high"; `wednesday doctor` lists active/unavailable protections |
| Cancellation / emergency stop | ✅ | Tests: cancel mid-stream, `/api/stop` |
| Crash isolation | ✅ | `kill -9` backend → avatar shows "brain offline" and recovers; `kill -9` avatar → backend keeps answering; llama-server dies with the backend (VRAM back to 16 MiB) |
| Install / uninstall | ✅ | Uninstall (keeping models) → `./wednesday` reinstall → running in 22 s; setup re-run is idempotent (19 s, checksums verified) |
| Secrets not committed | ✅ | `git add --dry-run` shows no token, `.env`, venv, model, or db |
| Hyprland keybinds/autostart | ⚙ ready, **not installed** | `./scripts/hyprland.sh install` checks conflicts and backs up config. Left for you to decide |

## Measured performance

| Metric | Value |
|---|---|
| `wednesday` → both processes up | **≈1.0 s** |
| Model load (warm shader cache) | 2.0–2.5 s, then 0.5–0.85 s prompt-cache warm-up |
| 20 back-to-back short replies (complete answer) | min 0.75 s · **median 1.43 s** · mean 1.58 s · max 3.91 s |
| Prompt processing per follow-up | ≈100 ms (≈108 new tokens; the cached prefix is reused) |
| Generation speed | 40–60 tok/s (62 tok/s raw; ~41–46 with Qwen's penalty sampling) |
| Thinking-mode answers | 9–30 s (budget 512 thinking tokens; thoughts stream live in the panel) |
| VRAM, model loaded (16K context) | **1.55 GB** of 4 GB, flat across 20 requests |
| VRAM, unloaded / after crash | 16 MiB |
| Idle CPU (1 core = 100%) | backend 0.1% · llama-server 0.5–0.8% · avatar **7.2%** · asleep **4.6%** |
| Idle GPU | 0% util, 1.9–2.3 W |
| Under load (20 requests) | GPU avg 29% / max 80%, max 59 °C, max 42 W; avatar ≈25% CPU while animating at 30 fps |
| RSS | backend 235 MB (+0.8 MB after 20 requests) · avatar 84–99 MB · llama-server 840 MB → 1.1 GB (prompt cache, capped at 256 MB) |
| Speech-to-text | ≈1.2 s per 4 s utterance (CPU, base.en, int8) |
| Text-to-speech | ≈1 s per sentence (CPU); pipelined so sentences play back-to-back |

## Known limitations (honest)

1. **A 2B model is a 2B model.** It answers chat and simple technical questions well, but in our small test it got the bat-and-ball and widget puzzles wrong at times. It also sometimes invents playful "experiences" ("the coffee machine"), and occasionally explores with tools more than needed. The guards (router, intent gating, runaway stop, ReplyFilter) reduce this but don't eliminate it. For harder work, try `model.active: qwen35-2b-q8`, or add a 4B profile (Qwen3.5-4B Q4 ≈ 2.7 GB fits).
2. **Web search without a key is best-effort.** DuckDuckGo answers automated clients with HTTP 202, and Wikipedia's API rejects scripts without contact info. Wednesday won't impersonate a browser to get around either. For reliable search, get a free Brave Search API key (put `BRAVE_API_KEY` in your environment and set `web.provider: brave`), or run SearXNG and set `web.searxng_url`. Optionally set `web.contact` (your email or URL) to satisfy Wikimedia's policy; I did not add your email.
3. **Voice jargon.** Whisper base.en heard "ROS two workspace" as "artist who works pays" even with vocabulary priming. Ordinary speech is accurate. `small.en` would be more accurate but about 3× slower and is not installed.
4. **Hermes Agent is not used** (see ARCHITECTURE ADR-1). If you later get an 8 GB+ GPU, Hermes could drive the same llama-server via its OpenAI-compatible provider.
5. **The avatar is procedural vector art**, not a rigged Live2D model. Expressions, blinking, breathing, gaze, lip-sync, and effects are real; there's no full-body motion. The event protocol is designed so a Live2D or 3D renderer can be swapped in without backend changes.
6. **No OS sandbox** (bubblewrap) around approved commands yet. The permission layer and your approval are the controls.
7. **Temperatures:** GPU limits come from the NVIDIA driver's "GPU Slowdown Temp" and CPU limits from coretemp's "high" value. No universal thresholds were invented, and no software can guarantee zero throttling.

## Please verify (things I couldn't exercise without a pointer device)

- Drag her to a new spot, then run `wednesday restart`: she should come back in the same place.
- Ctrl+scroll over her to resize.
- Right-click menu → each item.
- Click into the panel's text box and type. The panel takes the keyboard only while it's focused; Esc closes it.

## Files

- `wednesday`: launcher
- `src/wednesday/`: code (6.8k lines)
- `tests/`: 53 tests
- `config/`: defaults and permissions
- `scripts/`: setup, doctor, uninstall, hyprland, systemd unit
- `docs/`: these documents

**Data:**
- `~/.local/share/wednesday`: models, runtime, voices, memory.db
- `~/.local/state/wednesday`: logs, token, pids
