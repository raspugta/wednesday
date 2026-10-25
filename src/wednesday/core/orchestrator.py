"""The agent loop. Hermes-inspired, sized for a 2B model on a 4 GB GPU.

What we borrowed from Hermes Agent's design (not its code — Hermes wants ≥64K context):
  * a tool loop where the model plans, calls tools, reads results, and continues;
  * persistent memory injected as a small "things you remember" block;
  * context compression: when history overflows the budget, the oldest turns are summarised
    by the model itself and replaced with that summary.
What we changed for small models:
  * one tool call per round (parallel_tool_calls=false) — 2B models fumble parallel calls;
  * hard per-round and per-result limits; older tool outputs are shrunk aggressively;
  * thinking only when the request looks like it needs it, with a token budget.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from wednesday.agent.permissions import Permissions
from wednesday.agent.tools import ToolContext, ToolExecutor, describe_call, tool_schemas
from wednesday.config import Config
from wednesday.core import persona
from wednesday.core.event_bus import EventBus
from wednesday.core.model_manager import ModelError, ModelManager
from wednesday.core.resource_guardian import ResourceGuardian
from wednesday.core.state import AvatarState, StateController
from wednesday.core.text import (ReplyFilter, SentenceChunker, estimate_tokens, message_tokens,
                                 needs_thinking, needs_tools, truncate_middle)
from wednesday.memory.store import MemoryStore

log = logging.getLogger(__name__)

# "remember that X" is handled by code, not by the model: a 2B model given this as a tool call
# often wanders off instead. Deterministic saving is reliable and the user sees exactly what was stored.
_REMEMBER_CMD = re.compile(r"^\s*(?:please\s+|hey\s+wednesday,?\s+)?(?:remember|note|make a note|don'?t forget|"
                           r"keep in mind)(?:\s+that|\s*:|,)?\s+(?P<what>.{4,})$", re.I | re.S)
_PREFERENCE = re.compile(r"\b(i (prefer|like|love|hate|use|always|never|want)|my favou?rite|call me)\b", re.I)
_ABOUT_SELF = re.compile(r"\b(you|your|feel|feeling|hot|warm|temp|temperature|thermal|cpu|gpu|vram|ram|memory usage|"
                         r"load|status|laptop|machine|fan|tired|ok\??|okay\??)\b", re.I)
def third_person(text: str) -> str:
    """Store "my workspace is X, I use Jazzy" as "the user's workspace is X, the user uses Jazzy":
    first-person memories get parroted back by small models as if they were about themselves."""
    t = re.sub(r"\bI'm\b", "the user is", text)
    t = re.sub(r"\bI've\b", "the user has", t)
    t = re.sub(r"\bI (\w+?)(e?)\b", lambda m: "the user " + _third_verb(m.group(1) + m.group(2)), t)
    t = re.sub(r"\bmy\b", "the user's", t, flags=re.I)
    t = re.sub(r"\b(me|myself)\b", "the user", t)
    return t


def _third_verb(v: str) -> str:
    irregular = {"am": "is", "have": "has", "do": "does", "go": "goes", "was": "was", "can": "can",
                 "will": "will", "should": "should", "don't": "doesn't", "prefer": "prefers"}
    if v in irregular:
        return irregular[v]
    if v.endswith(("s", "sh", "ch", "x", "o")):
        return v + "es"
    if v.endswith("y") and len(v) > 2 and v[-2] not in "aeiou":
        return v[:-1] + "ies"
    return v + "s"


_TOOL_INTENT = {
    "remember": re.compile(r"\b(remember|memori[sz]e|note (that|this|down)|make a note|don'?t forget|keep in mind|"
                           r"save (this|that)|for future reference)\b", re.I),
    "write_file": re.compile(r"\b(write|create|save|edit|modify|append|update|generate|make)\b.*\b(file|script|"
                             r"\.\w{1,4}|readme|code|config|note)\b|\b(write|create|save) (it|that|this)\b", re.I),
    "plan_task": re.compile(r"\b(plan|steps|break (it|this|that) down|todo|to-do|checklist|roadmap)\b", re.I),
    "update_task": re.compile(r"\b(task|step|done|finished|completed|mark)\b", re.I),
    "open_in_browser": re.compile(r"\b(open|show (it|me|this) in|browser|launch)\b", re.I),
    "web_search": re.compile(r"\b(search|look ?up|google|web|internet|online|latest|newest|news|today|current|"
                             r"weather|price|release|who is|what happened)\b", re.I),
    "read_webpage": re.compile(r"https?://|\b(search|look ?up|web|internet|online|latest|news|page|site|article|docs?)\b", re.I),
}


def _tool_intended(name: str, text: str) -> bool:
    pattern = _TOOL_INTENT.get(name)
    return pattern is None or bool(pattern.search(text))


_ASKED_TO_REMEMBER = re.compile(r"\b(remember|memori[sz]e|note (that|this|down)|make a note|don'?t forget|"
                                r"keep in mind|save (this|that)|for future reference)\b", re.I)
TOOL_RESULT_LIMIT = 6000      # chars of a fresh tool result shown to the model
OLD_TOOL_RESULT_LIMIT = 700   # chars kept once that result is from an earlier turn
APPROVAL_TIMEOUT_S = 180


@dataclass
class Conversation:
    id: str
    messages: list[dict] = field(default_factory=list)
    summary: str = ""
    last_used: float = field(default_factory=time.time)


@dataclass
class Job:
    id: str
    text: str
    session: str
    think: str            # auto | fast | deep
    source: str           # panel | cli | voice
    created: float = field(default_factory=time.time)


class Orchestrator:
    def __init__(self, cfg: Config, bus: EventBus, model: ModelManager, guardian: ResourceGuardian,
                 memory: MemoryStore | None, states: StateController):
        self.cfg = cfg
        self.bus = bus
        self.model = model
        self.guardian = guardian
        self.memory = memory
        self.states = states
        self.perms = Permissions(cfg.permissions)
        self.settings = {
            "online": bool(cfg.get("web.online")),
            "speak": bool(cfg.get("voice.speak_replies")),
            "thinking": str(cfg.get("model.thinking")),
            "memory": bool(cfg.get("memory.enabled")) and memory is not None,
        }
        self.tools = ToolExecutor(ToolContext(
            cfg=cfg, perms=self.perms, bus=bus, memory=memory, guardian=guardian,
            request_approval=self.request_approval, is_online=lambda: self.settings["online"]))
        self.conversations: dict[str, Conversation] = {}
        self.queue: asyncio.Queue[Job] = asyncio.Queue(int(cfg.get("resources.max_queue")))
        self.current: Job | None = None
        self._current_task: asyncio.Task | None = None
        self._worker: asyncio.Task | None = None
        self._approvals: dict[str, asyncio.Future] = {}
        self.speaker = None   # set by the voice module: has feed(str), flush(), stop()

    # ------------------------------------------------------------------ public API
    def start(self) -> None:
        self._worker = asyncio.create_task(self._work())
        self.bus.publish("settings", **self.settings)

    async def stop(self) -> None:
        await self.emergency_stop(quiet=True)
        if self._worker:
            self._worker.cancel()

    def update_settings(self, **changes) -> dict:
        for k, v in changes.items():
            if k in self.settings and v is not None:
                if k == "thinking" and v not in ("auto", "fast", "deep"):
                    raise ValueError("thinking must be auto|fast|deep")
                if k == "memory" and v and self.memory is None:
                    raise ValueError("memory store is not available")
                self.settings[k] = v if k == "thinking" else bool(v)
        self.bus.publish("settings", **self.settings)
        return self.settings

    def submit(self, text: str, session: str = "desktop", think: str | None = None, source: str = "panel") -> Job:
        text = text.strip()
        if not text:
            raise ValueError("empty message")
        if len(text) > 20_000:
            raise ValueError("message too long (20k chars max)")
        ok, why = self.guardian.admit()
        if not ok:
            self.states.set(AvatarState.ERROR, "concerned")
            self.bus.publish("quip", text=persona.quip("pressure", self.humor) or why)
            raise RuntimeError(why)
        job = Job(uuid.uuid4().hex[:12], text, session, think or self.settings["thinking"], source)
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            raise RuntimeError("Too many requests queued — give me a second.") from None
        self.guardian.queue_length = self.queue.qsize()
        self.bus.publish("job_queued", job=job.id, session=session, text=text if source != "voice" else text, source=source)
        return job

    def cancel(self, job_id: str | None = None) -> bool:
        if self._current_task and not self._current_task.done() and (job_id is None or
                                                                   (self.current and self.current.id == job_id)):
            self._current_task.cancel()
            return True
        return False

    async def emergency_stop(self, quiet: bool = False) -> dict:
        from wednesday.tools import shell
        dropped = 0
        while not self.queue.empty():
            self.queue.get_nowait()
            dropped += 1
        self.guardian.queue_length = 0
        cancelled = self.cancel()
        for fut in self._approvals.values():
            if not fut.done():
                fut.set_result(False)
        killed = shell.kill_all()
        if self.speaker:
            self.speaker.stop()
        if not quiet:
            self.bus.publish("emergency_stop", cancelled=cancelled, dropped=dropped, killed=killed)
            self.states.set(AvatarState.IDLE, "calm")
        return {"cancelled": cancelled, "dropped_queue": dropped, "killed_processes": killed}

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        fut = self._approvals.get(approval_id)
        if not fut or fut.done():
            return False
        fut.set_result(bool(approved))
        return True

    def reset(self, session: str = "desktop") -> None:
        self.conversations.pop(session, None)
        self.bus.publish("conversation_reset", session=session)

    @property
    def humor(self) -> float:
        return float(self.cfg.get("assistant.humor", 0.7))

    # ------------------------------------------------------------------ approvals
    async def request_approval(self, info: dict) -> bool:
        aid = uuid.uuid4().hex[:10]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._approvals[aid] = fut
        self.states.set(AvatarState.WORKING, "curious", "waiting for your approval")
        self.bus.publish("approval_request", id=aid, job=self.current.id if self.current else None,
                         session=self.current.session if self.current else None,
                         quip=persona.quip("approval", self.humor), **info)
        try:
            approved = await asyncio.wait_for(fut, APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            approved = False
        finally:
            self._approvals.pop(aid, None)
        self.bus.publish("approval_resolved", id=aid, approved=approved)
        log.info("approval %s for %s: %s", aid, info.get("tool"), "approved" if approved else "denied")
        return approved

    # ------------------------------------------------------------------ worker
    async def _work(self) -> None:
        while True:
            job = await self.queue.get()
            self.guardian.queue_length = self.queue.qsize()
            self.current = job
            self._current_task = asyncio.create_task(self._run_job(job))
            try:
                await self._current_task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                log.exception("job crashed")
            finally:
                self.current = None
                self._current_task = None

    def _conversation(self, session: str) -> Conversation:
        conv = self.conversations.get(session)
        if conv is None:
            conv = self.conversations[session] = Conversation(session)
        conv.last_used = time.time()
        return conv

    def _system_message(self) -> dict:
        return {"role": "system", "content": persona.system_prompt(
            name=self.cfg.get("assistant.name"), humor=self.humor, online=self.settings["online"])}

    def _context(self, conv: Conversation, user_text: str) -> str:
        memories: list[str] = []
        if self.settings["memory"] and self.memory:
            limit = int(self.cfg.get("memory.max_injected"))
            seen = set()
            for m in self.memory.pinned(2) + self.memory.search(user_text, limit):
                if m["id"] in seen:
                    continue
                seen.add(m["id"])
                memories.append(f"[{m['kind']}] {m['content'][:300]}")
                if len(memories) >= limit:
                    break
        # Sensor readings only when the user is asking about her or the machine; otherwise a small
        # model fixates on them ("your GPU is hot!") instead of answering the question.
        body = persona.body_feeling(self.guardian.snap) if _ABOUT_SELF.search(user_text) and \
            re.search(r"\b(how are you|feel|hot|temp|thermal|cpu|gpu|vram|ram|status|laptop|machine|fan|load)\b",
                      user_text, re.I) else ""
        return persona.context_block(memories=memories, summary=conv.summary, body=body)

    def _messages_for_model(self, conv: Conversation, system: dict, user_text: str) -> list[dict]:
        """System + history, with the volatile context block prepended to a *copy* of the newest
        user message. Stored history stays clean, so the cached prefix survives the next turn."""
        msgs = [system, *persona.few_shot_messages(), *conv.messages]
        for i in range(len(msgs) - 1, len(msgs) - len(conv.messages) - 1, -1):
            if msgs[i]["role"] == "user":
                msgs[i] = {**msgs[i], "content": self._context(conv, user_text) + "\n\n" + msgs[i]["content"]}
                break
        return msgs

    def _explicit_remember(self, text: str) -> int | None:
        m = _REMEMBER_CMD.match(text)
        if not m or not (self.settings["memory"] and self.memory):
            return None
        what = m.group("what").strip().rstrip(".!")
        kind = "preference" if _PREFERENCE.search(what) else "note"
        what = third_person(what)
        mem_id = self.memory.add(what, kind, source="user")
        self.bus.publish("memory_saved", id=mem_id, content=what[:200], kind=kind)
        return mem_id

    def _wants_tools(self, conv: Conversation, text: str) -> bool:
        """Tool router. 2B models call tools for small talk if you let them, so tools are only
        offered when the request plausibly needs one, or right after a turn that used tools
        (follow-ups like "now run it"). Setting model.tools: always disables the router."""
        if self.cfg.get("model.tools", "auto") == "always":
            return True
        recent = conv.messages[-6:-1]
        if any(m["role"] == "tool" for m in recent):
            return True
        return needs_tools(text)

    def _budget(self, tools: list[dict], system: dict) -> int:
        ctx = int(self.cfg.get("model.ctx_size"))
        fixed = estimate_tokens(str(tools)) + message_tokens(system) + int(self.cfg.get("model.max_tokens"))
        return max(1500, ctx - fixed - 600)

    async def _compress(self, conv: Conversation, budget: int) -> None:
        """Summarise the oldest turns when history exceeds the budget (Hermes-style compression)."""
        total = sum(message_tokens(m) for m in conv.messages)
        if total <= budget:
            return
        # Cut at a user-message boundary so no tool result is orphaned from its call.
        target = budget * 0.55
        cut = 0
        running = total
        for i, m in enumerate(conv.messages):
            if running <= target and m["role"] == "user" and i > 0:
                cut = i
                break
            running -= message_tokens(m)
        else:
            cut = max(i for i, m in enumerate(conv.messages) if m["role"] == "user")
        if cut <= 0:
            return
        old, conv.messages = conv.messages[:cut], conv.messages[cut:]
        transcript = "\n".join(
            f"{m['role']}: {truncate_middle(m.get('content') or '', 600)}" for m in old if m.get("content"))
        prompt = [
            {"role": "system", "content": "You compress conversations. Write a factual summary in under 120 words: "
                                          "user goals, decisions, facts learned, open questions. No mood tags."},
            {"role": "user", "content": (f"Previous summary: {conv.summary}\n\n" if conv.summary else "") + transcript},
        ]
        self.bus.publish("context_compressed", dropped_messages=len(old))
        try:
            summary = await self.model.complete(prompt, max_tokens=220)
        except (ModelError, OSError) as e:
            log.warning("summary failed (%s); dropping old turns without summary", e)
            return
        conv.summary = summary[:1200]
        if self.settings["memory"] and self.memory and self.cfg.get("memory.save_summaries"):
            self.memory.add(conv.summary, "summary", source="compression")

    @staticmethod
    def _shrink_old_tool_results(conv: Conversation) -> None:
        last_user = max((i for i, m in enumerate(conv.messages) if m["role"] == "user"), default=0)
        for m in conv.messages[:last_user]:
            if m["role"] == "tool" and len(m.get("content") or "") > OLD_TOOL_RESULT_LIMIT:
                m["content"] = truncate_middle(m["content"], OLD_TOOL_RESULT_LIMIT)

    async def _run_job(self, job: Job) -> None:
        conv = self._conversation(job.session)
        self.states.busy = True
        self.model.busy = True
        self.guardian.active = True
        self.guardian.active_inference += 1
        woke = self.states.wake()
        mood_guess = persona.infer_mood(job.text)
        self.states.set(AvatarState.THINKING, mood_guess)
        self.bus.publish("job_started", job=job.id, session=job.session)
        if woke:
            self.bus.publish("quip", text=persona.quip("wake", self.humor))
        thinking = job.think == "deep" or (job.think == "auto" and needs_thinking(job.text))
        reply_text = ""
        used_tools = False
        mood: str | None = None
        t0 = time.monotonic()
        conv.messages.append({"role": "user", "content": job.text})
        try:
            if self.model.status.state != "ready":
                self.bus.publish("quip", text=persona.quip("loading", self.humor))
                await self.model.ensure_loaded()
            tools = tool_schemas(self.settings["online"], self.settings["memory"])
            # Offer only the tools this request plausibly needs: a 2B model handed write/plan tools
            # "helpfully" invents work nobody asked for.
            intent_text = " ".join(m["content"] for m in conv.messages[-5:] if m["role"] == "user")
            tools = [t for t in tools if _tool_intended(t["function"]["name"], intent_text)]
            if not self._wants_tools(conv, job.text):
                tools = []
            if (saved := self._explicit_remember(job.text)) is not None:
                tools = []  # nothing left to do but acknowledge
                conv.messages[-1] = {"role": "user", "content": job.text +
                                     f"\n[system note: this was saved to long-term memory as #{saved}. "
                                     "Acknowledge briefly; do not call tools.]"}
            failures: dict[str, int] = {}
            for round_no in range(int(self.cfg.get("model.max_tool_rounds")) + 1):
                system = self._system_message()
                self._shrink_old_tool_results(conv)
                await self._compress(conv, self._budget(tools, system))
                # On the final allowed round, withhold tools so the model must answer.
                offer_tools = (tools or None) if round_no < int(self.cfg.get("model.max_tool_rounds")) else None
                content, calls, mood_seen = await self._stream_round(
                    job, self._messages_for_model(conv, system, job.text), offer_tools,
                    thinking and round_no == 0, True)  # every round may open with a tag: always strip it
                if mood_seen:
                    mood = mood_seen
                    self.states.set_mood(mood)
                reply_text += content
                if calls and used_tools and len(content.strip()) >= 60:
                    # Runaway guard: she already answered using tool results; further tool calls in
                    # the same turn are unrequested follow-up work. Stop and let the user decide.
                    log.info("dropping %d unrequested follow-up tool call(s)", len(calls))
                    calls = []
                if not calls:
                    conv.messages.append({"role": "assistant", "content": content})
                    break
                used_tools = True
                conv.messages.append({"role": "assistant", "content": content, "tool_calls": calls})
                for call in calls:
                    name = call["function"]["name"]
                    raw = call["function"].get("arguments") or "{}"
                    try:
                        summary = describe_call(name, json.loads(raw))
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        summary = name
                    self.states.set(AvatarState.WORKING, None, summary)
                    self.bus.publish("tool_start", job=job.id, tool=name, summary=summary, call_id=call["id"])
                    result = await self.tools.execute(name, raw)
                    ok = not result.startswith(("ERROR", "BLOCKED", "DENIED"))
                    self.bus.publish("tool_end", job=job.id, tool=name, summary=summary, call_id=call["id"],
                                     ok=ok, preview=result[:400])
                    if not ok:
                        failures[name] = failures.get(name, 0) + 1
                        if failures[name] >= 2:
                            # Small models retry a failing tool forever. Two strikes: withdraw it.
                            tools = [t for t in tools if t["function"]["name"] != name]
                            result += (f"\n[{name} has failed twice and is now unavailable for this request. "
                                       "Answer with what you have and tell the user what failed.]")
                    conv.messages.append({"role": "tool", "tool_call_id": call["id"],
                                          "content": truncate_middle(result, TOOL_RESULT_LIMIT)})
                    self.states.set(AvatarState.THINKING)
            final_mood = mood or persona.infer_mood(job.text, reply_text)
            elapsed = round(time.monotonic() - t0, 2)
            if self.speaker and self.settings["speak"]:
                self.speaker.flush()
            self.bus.publish("reply_done", job=job.id, session=job.session, text=reply_text.strip(),
                             mood=final_mood, elapsed_s=elapsed, stats=self.model.status.stats,
                             thinking=thinking, used_tools=used_tools)
            if self.cfg.get("logging.log_content"):
                log.info("reply (%ss): %s", elapsed, reply_text[:500])
            else:
                log.info("job %s done in %ss (%s chars, tools=%s)", job.id, elapsed, len(reply_text), used_tools)
            self.states.busy = False
            self.states.set(AvatarState.SUCCESS if used_tools else AvatarState.IDLE, final_mood)
            if not used_tools:
                self.states.settle()
        except asyncio.CancelledError:
            conv.messages.append({"role": "assistant", "content": (reply_text + " [interrupted]").strip()})
            if self.speaker:
                self.speaker.stop()
            self.bus.publish("cancelled", job=job.id, quip=persona.quip("cancel", self.humor))
            self.states.busy = False
            self.states.set(AvatarState.IDLE, "calm")
            raise
        except (ModelError, OSError, RuntimeError) as e:
            log.error("job %s failed: %s", job.id, e)
            if conv.messages and conv.messages[-1]["role"] == "user":
                conv.messages.pop()
            self.bus.publish("error", job=job.id, message=str(e)[:600], quip=persona.quip("error", self.humor))
            self.states.busy = False
            self.states.set(AvatarState.ERROR, "sheepish")
        finally:
            self.states.busy = False
            self.model.busy = False
            self.guardian.active_inference -= 1
            self.guardian.active = False
            self.states.touch()

    async def _stream_round(self, job: Job, messages: list[dict], tools: list[dict] | None,
                            thinking: bool, want_mood: bool) -> tuple[str, list[dict], str | None]:
        reply = ReplyFilter(persona.MOODS, want_mood)
        calls: dict[int, dict] = {}
        chunker = SentenceChunker() if (self.speaker and self.settings["speak"]) else None
        speaking = False
        announced_mood = False

        def emit(text: str) -> None:
            nonlocal speaking, announced_mood
            if reply.retracted:
                reply.retracted = False
                self.bus.publish("retract", job=job.id)
                if chunker:
                    chunker.buf = ""
            if reply.mood and not announced_mood:
                announced_mood = True
                self.states.set_mood(reply.mood)
            if not text:
                return
            if not speaking:
                speaking = True
                self.states.set(AvatarState.SPEAKING)
            self.bus.publish("token", job=job.id, text=text)
            if chunker:
                for sentence in chunker.feed(text):
                    self.speaker.feed(sentence)

        # Stop strings: an invented next turn means the model is rambling; a raw <tool_call> when
        # no tools were offered is a hallucinated call that must not reach the user.
        stop = ["\nUser:", "\nuser:", "<|im_start|>"] + ([] if tools else ["<tool_call>", "<function="])
        # aclosing: breaking out early must close the HTTP stream so llama-server stops generating.
        async with contextlib.aclosing(self.model.stream_chat(messages, tools, thinking=thinking, stop=stop)) as stream:
            async for d in stream:
                if d.reasoning:
                    self.bus.publish("thinking", job=job.id, text=d.reasoning)
                if d.tool_calls:
                    for tc in d.tool_calls:
                        slot = calls.setdefault(tc.get("index", 0), {
                            "id": tc.get("id") or uuid.uuid4().hex[:16], "type": "function",
                            "function": {"name": "", "arguments": ""}})
                        fn = tc.get("function") or {}
                        slot["function"]["name"] += fn.get("name") or ""
                        slot["function"]["arguments"] += fn.get("arguments") or ""
                if d.content:
                    emit(reply.feed(d.content))
                    if reply.done:
                        break
        emit(reply.finish())
        if chunker:
            for sentence in chunker.flush():
                self.speaker.feed(sentence)
        return reply.content, [calls[k] for k in sorted(calls) if calls[k]["function"]["name"]], reply.mood

    async def warm_up(self) -> float:
        """Pre-fill the KV cache with the static prompt + tool schemas (and compile any GPU
        shaders on first ever run) so the user's first message is answered instantly."""
        t0 = time.monotonic()
        msgs = [self._system_message(), *persona.few_shot_messages(), {"role": "user", "content": "hi"}]
        tools = tool_schemas(self.settings["online"], self.settings["memory"])
        stats = self.model.status.stats
        async for _ in self.model.stream_chat(msgs, tools, thinking=False, max_tokens=1):
            pass
        self.model.status.stats = stats  # a 1-token warm-up isn't a meaningful speed reading
        return round(time.monotonic() - t0, 2)

    # ------------------------------------------------------------------ housekeeping
    async def housekeeping(self) -> None:
        """Runs every 20s: sleep the avatar when idle, occasional idle quip, memory retention."""
        last_quip = time.time()
        last_retention = 0.0
        while True:
            await asyncio.sleep(20)
            try:
                self.states.maybe_sleep(self.model.status.state == "ready")
                minutes = float(self.cfg.get("assistant.idle_quip_minutes") or 0)
                if (self.cfg.get("assistant.idle_quips") and minutes and self.states.state == AvatarState.IDLE
                        and time.time() - last_quip > minutes * 60 and self.bus.subscriber_count):
                    last_quip = time.time()
                    if q := persona.quip("idle", self.humor):
                        self.bus.publish("quip", text=q)
                if self.memory and time.time() - last_retention > 3600:
                    last_retention = time.time()
                    n = self.memory.apply_retention(float(self.cfg.get("memory.retention_days")))
                    if n:
                        log.info("retention removed %d old memories", n)
                for sid, conv in list(self.conversations.items()):
                    if time.time() - conv.last_used > 6 * 3600 and sid != "desktop":
                        del self.conversations[sid]
            except Exception:
                log.exception("housekeeping failed")
