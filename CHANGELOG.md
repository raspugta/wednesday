# Changelog

## 0.1.0 (2026-10-24): first working build

Decisions and fixes in the order they happened, so the reasoning is preserved.

**Foundation**
- Inspected the machine first: i5-13420H, 15 GB RAM, RTX 3050 4 GB (driver 595.99, CUDA 13.2), Ubuntu 26.04.1, Hyprland 0.53.3, Python 3.14, PipeWire, gtk-layer-shell already installed.
- Hermes Agent recommends ≥64K context and 8 GB+ GPUs. Instead of running it, built our own agent loop using its design ideas.
- Picked the llama.cpp b11853 **Vulkan** build (35 MB) over CUDA (770 MB), and selected the NVIDIA device by *name*, because the Intel iGPU enumerates first.
- Chose Qwen3.5-2B Q4_K_M. Q8 was A/B-tested and wasn't better on our eval.

**Speed**
- Found one-time Vulkan shader compilation: a 276-token prompt took 44 s cold and 1 s warm. Setup now pre-compiles shaders.
- Capped llama-server's host prompt cache at 256 MB (the default is 8 GB of RAM).
- Made the system prompt static and moved time, memories, and sensor readings onto the newest message. Follow-up prompt processing dropped from about 7.5 s to about 0.1 s.
- Added a thinking budget (512 tokens): a 2B model otherwise spent 600+ tokens deliberating over bat-and-ball.

**Reply quality on a 2B model**
- Switched to Qwen's recommended sampling (top_k 20, presence 1.5). That cut rambling; the cost is about 41–46 tok/s instead of 62, from CPU-side penalty sampling over a 248K vocabulary.
- Moved the few-shot style examples from inline text into real message pairs, because inline examples were copied verbatim.
- Added `ReplyFilter`: strips mood tags (including invented ones), retracts `</think>` leaks, cuts second replies or invented "User:" turns, and holds back partial tags.
- Added a tool router plus per-tool intent gating, after measuring web searches for jokes and unrequested README writes.
- Added a runaway guard: no unrequested tool calls after an answer. Tools that fail twice are withdrawn for the rest of the request.
- Made "remember X" deterministic and stored in the third person, after the model parroted "my workspace" as its own.
- Real sensor readings are now injected *only* when you ask about her or the machine (she invented a 68 °C GPU otherwise).

**Desktop**
- Layer-shell avatar with a silhouette-only input region. Click-through verified with real cursor movement (`hyprctl dispatch movecursor`) and pointer-enter events.
- Panel bug: a background-clear draw handler leaked the CLEAR operator to every child widget. Fixed with save/restore.
- Idle CPU 16% → 7.2% (no off-screen group when not dimmed, batched strokes, 5 fps idle with bursts). Sleep 26% → 4.6% (the "z" particles were forcing 30 fps).

**Web**
- An IPv6 NAT64 network made the SSRF guard reject DuckDuckGo (`64:ff9b::/96`); public-IP checks now look at the embedded IPv4.
- DuckDuckGo answers automated clients with HTTP 202, and Wikimedia rejects scripts that give no contact info. We do **not** disguise the client. Fallback order is: DuckDuckGo HTML → DuckDuckGo Instant Answer API + Wikipedia, with honest errors; Brave/SearXNG for reliable search.
- Fixed `StreamReader.read(n)` returning only the first chunk, and raised the header limit for GitHub's 8 KB+ CSP header.

**Voice**
- faster-whisper's PyAV decoder breaks on the installed PyAV (`metadata_errors`). WAV is now decoded with the stdlib.
- The Piper voice is preloaded in the background, removing the 3 s first-utterance delay.
- Fixed the TTS chunker for code fences (both a lookahead `endpos` bug and splitting inside code).

**Reliability**
- pidfile-based orphan reclaim (it only ever kills our own binary).
- `PR_SET_PDEATHSIG`: llama-server dies with the backend, so VRAM can't be stranded by a crash.
- Approvals are scoped to their job, so one client can't answer another's request.
