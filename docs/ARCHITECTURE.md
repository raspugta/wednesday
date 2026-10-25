# Architecture

```
 ┌──────────────── wednesday ui (GTK3 process) ─────────────────┐
 │  AvatarWindow  layer-shell OVERLAY, keyboard NONE,            │
 │                input region = silhouette only                 │
 │  Panel         layer-shell OVERLAY, keyboard ON_DEMAND        │
 │  Renderer      procedural cairo vector art (no GTK; testable) │
 │  Bridge        asyncio thread ⇄ GLib main loop, auto-reconnect│
 └───────────────▲──────────────────────────────────────────────┘
                 │ HTTP + WebSocket on 127.0.0.1:7377 (bearer token, no Origin, Host check)
 ┌───────────────┴──────────── wednesday backend (asyncio) ──────┐
 │  api/server        REST commands + /api/events stream          │
 │  core/orchestrator agent loop, router, compression, approvals  │
 │  core/state        avatar state machine (driven by real events)│
 │  core/persona      prompt, few-shot, moods, quips              │
 │  core/text         ReplyFilter, sentence chunker, routers      │
 │  core/model_manager owns the single llama-server process       │
 │  core/resource_guardian  sensors + admission policy            │
 │  agent/permissions READ / APPROVAL / BLOCKED decisions         │
 │  agent/tools       schemas, validation, executors              │
 │  tools/{files,shell,web}  the I/O itself                       │
 │  memory/store      SQLite + FTS5 (memories, tasks)             │
 │  voice/audio       pw-record → faster-whisper; Piper → pw-cat  │
 └───────────────┬───────────────────────────────────────────────┘
                 │ OpenAI-compatible HTTP on 127.0.0.1:18181 (random api-key per launch)
        llama-server (llama.cpp b11853, Vulkan, RTX 3050) — dies with its parent
```

The backend never imports GTK, and the UI never imports the model, so either can crash without taking down the other. Both cases were tested with `kill -9`.

## Decisions (ADR summary)

| # | Decision | Why | Alternatives rejected |
|---|---|---|---|
| 1 | **Own agent loop, Hermes-inspired** instead of Hermes Agent itself | Hermes' local-model docs: "Every recommended model gets at least a 64K context window," with 8 GB+ GPUs recommended. On 4 GB with a 2B model that doesn't fit, and Hermes pulls in a gateway/dashboard stack we don't need. We kept its *ideas*: a tool loop, memory injection, and context compression by self-summary. | Running Hermes against llama-server at a reduced context would silently violate its stated requirements. |
| 2 | **llama.cpp b11853 Vulkan build** (35 MB, official release) | Your driver supports CUDA 13.2; the CUDA build is 770 MB with cudart. Vulkan on the NVIDIA ICD measured 60+ tok/s generation and about 1,100 tok/s prompt processing. A single binary, managed as a child process: unloading means killing it, which is the only *guaranteed* way to return VRAM. | Ollama (a second daemon plus ~1.5 GB of bundled CUDA), the CUDA build, AirLLM (layer-streaming for models that *don't* fit, so no benefit here). |
| 3 | **Qwen3.5-2B Q4_K_M** | Your ask: best 2B that reasons, fast. Hybrid attention means a small KV cache (16K context costs about 1.5 GB VRAM total), toggleable thinking, native tool calls, Apache-2.0. Q8 was downloaded and A/B-tested: slower and not measurably better on our small eval. | Qwen3-4B-2507 (slower, no hybrid thinking), Gemma 4 E2B (comparable; Qwen chosen for tool-call template support in llama.cpp). |
| 4 | **Static system prompt + per-turn context block** | llama.cpp reuses the KV cache for an unchanged prefix. Time, memories, and sensors go on the newest user message only, so follow-ups process about 100 new tokens (about 100 ms) instead of 1,800. | Putting volatile data in the system prompt cost 7.5 s per turn. |
| 5 | **Tool router + per-tool intent gating** | A 2B model handed tools uses them for small talk and invents follow-up work. Tools are offered only for action, live-data, or path/URL requests, and write/plan/web tools only when the request implies them. | Always offering every tool (measured: web searches for jokes, unrequested README writes). |
| 6 | **Deterministic "remember …"** | The model often didn't call `remember` when asked. Code now saves it and the model just acknowledges, so you see exactly what was stored. | Trusting the model. |
| 7 | **GTK3 + gtk-layer-shell, cairo-drawn avatar** | Already installed on your system. Layer-shell gives above-windows, no-focus, and click-through without editing hyprland.conf. The vector avatar has no artwork licensing issues, scales freely, and makes expressions parameters rather than sprites. | Qt (no layer-shell bindings installed), a WebView (heavier, transparency/click-through unverifiable), Live2D (proprietary SDK license, needs a rigged model). |
| 8 | **SQLite FTS5 memory, no embeddings** | Instant, zero VRAM, injection-safe query building. Embeddings would need a second model resident beside the LLM on a 4 GB card. | Vector DB. |
| 9 | **Voice on CPU via PipeWire CLIs** | Keeps the whole GPU for the LLM. `pw-record`/`pw-cat` handle device routing, and killing them is an instant, reliable stop. | sounddevice/PortAudio (extra native deps). |

## Request lifecycle

1. `POST /api/chat` → admission check (RAM, GPU/CPU thermal margins, queue bound) → bounded queue (4).
2. Single worker: state THINKING (mood guessed from your text) → model loads if unloaded.
3. Tools chosen by the router and intent gates; thinking chosen by `thinking` mode plus a heuristic.
4. Stream: reasoning tokens → `thinking` events; content → `ReplyFilter` (mood tag, `</think>` leaks, rambling cut-offs, partial-tag hold-back) → `token` events → SPEAKING, and the TTS sentence chunker if speak is on.
5. Tool call → permission decision → approval card if needed (3 min timeout = deny) → execute → result (truncated) → next round. After two failures a tool is withdrawn. After a real answer, unrequested follow-up calls are dropped.
6. `reply_done` → SUCCESS (if tools were used) or IDLE. TTS keeps the state at SPEAKING until the audio ends.
7. History over budget → oldest turns summarised by the model (Hermes-style compression). If memory is on, the summary is saved as a `summary` memory.

## Resource behaviour

- **One model process** and one inference slot (`--parallel 1` plus the single worker).
- **Host prompt cache** capped at 256 MB (llama.cpp's default is 8 GB of RAM).
- **Idle unload** after 20 minutes. It reloads in about 2 s and is re-warmed automatically.
- **Avatar frame rate:**
  - 30 fps while animating
  - 5 fps when idle, bursting for blinks and eye movements
  - 2 fps when asleep
  - 0 fps when hidden
- **Whisper** is unloaded after 10 idle minutes.
- **Polling:** the Resource Guardian samples every 15 s when idle and every 3 s while working.
