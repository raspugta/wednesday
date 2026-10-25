"""Small text helpers shared by the orchestrator, voice and UI."""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+(?=[A-Z0-9\"'(\[`*])|\n{2,}")


def estimate_tokens(text: str) -> int:
    # Qwen's tokenizer averages ~3.5–4 chars/token on English + code; err on the high side.
    return max(1, int(len(text) / 3.3))


def message_tokens(msg: dict) -> int:
    n = 6 + estimate_tokens(msg.get("content") or "")
    for tc in msg.get("tool_calls") or []:
        n += estimate_tokens(tc.get("function", {}).get("arguments", "")) + 10
    return n


def for_speech(text: str) -> str:
    """Strip markdown so TTS reads prose, not syntax. Code blocks are skipped, not recited."""
    text = re.sub(r"```.*?```", " (code shown on screen) ", text, flags=re.S)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[*_#>|~]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


class SentenceChunker:
    """Feeds streamed text, yields complete sentences (for incremental TTS)."""

    def __init__(self, min_chars: int = 30):
        self.buf = ""
        self.min_chars = min_chars
        self.in_code = False

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out = []
        while True:
            # Never speak half a code block: only look for sentence ends before an unclosed fence.
            limit = len(self.buf)
            if self.buf.count("```") % 2 == 1:
                limit = self.buf.rfind("```")
            m = None
            for cand in _SENTENCE_END.finditer(self.buf):
                if cand.start() >= limit:
                    break  # (no endpos: the lookahead must still see the fence itself)
                if self.buf.count("```", 0, cand.start()) % 2 == 1:
                    continue  # inside a code block
                if cand.start() >= self.min_chars:
                    m = cand
                    break
            if not m:
                return out
            out.append(self.buf[: m.start()].strip())
            self.buf = self.buf[m.end():]

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n[... {len(text) - limit} chars omitted ...]\n" + text[-half:]


def needs_thinking(text: str) -> bool:
    """Heuristic for thinking=auto: reason only when the request smells like it needs it.
    Chit-chat stays on the fast path (sub-second first token)."""
    t = text.lower()
    if len(t) > 400:
        return True
    triggers = (
        r"\b(why|prove|derive|calculate|compute|solve|step[- ]by[- ]step|reason|analy[sz]e|compare|trade-?offs?|"
        r"debug|optimi[sz]e|algorithm|complexity|design|architect|plan|strategy|explain how|what if|"
        r"how many|how much|riddle|puzzle|math|equation|integral|probability|logic)\b"
    )
    if re.search(triggers, t):
        return True
    # multi-step arithmetic ("17*23 - 4^2") yes; "2+2" no
    return len(re.findall(r"\d+\s*[-+*/^]\s*(?=\d)", t)) >= 2


# Tools are offered only for an actual action, live data, or a concrete path/URL. Topic words
# alone ("explain a ROS 2 topic") must NOT trigger tools: a 2B model handed tools goes exploring.
_ACTION = re.compile(
    r"\b(run|execute|launch|open|read|show me|list|cat|grep|find|search|look ?up|google|browse|fetch|"
    r"check|inspect|scan|create|write|edit|modify|append|delete|remove|rename|move|copy|install|"
    r"build|compile|test it|run the tests|plan|break (it|this) down|recall|what do you remember|"
    r"git (status|log|diff|branch|show)|ls|cd)\b", re.I)
_LIVE = re.compile(r"\b(latest|newest|news|today'?s?|tonight|this week|right now|current(ly)?|weather|price|"
                   r"stock|score|released?|release date|cpu|gpu|vram|ram usage|temperature|disk space|"
                   r"system status|uptime|processes)\b", re.I)


def needs_tools(text: str) -> bool:
    if re.search(r"(^|\s)(~|/|\./)[\w.-]", text) or re.search(r"https?://", text):
        return True
    return bool(_ACTION.search(text) or _LIVE.search(text))


class ReplyFilter:
    """Turns the model's raw content stream into what the user should see.

    Handles, in one place:
      * the leading mood tag ("[amused] ...") → .mood, removed from the text
      * leading blank lines
      * "</think>" leaks (pseudo-thoughts closed in a non-thinking reply) → .retracted
      * a second mood tag on a new line = the model is starting another reply → .done
      * hold-back of a trailing partial tag ("\n[conce") so it never flashes on screen
    feed() returns only text that is safe to display now; finish() flushes the rest.
    """

    def __init__(self, moods: tuple[str, ...], want_mood: bool = True):
        self.moods = moods
        self.want_mood = want_mood
        self._second = re.compile(r"\n\s*\[(?:" + "|".join(moods) + r")\]|\n\s*(?:User|Human|You):", re.I)
        self._partial = re.compile(r"(\n\s*\[[A-Za-z]{0,12}|<(/t?h?i?n?k?)?|\n?User?:?)$")
        self.reset()

    def reset(self) -> None:
        self.raw = ""
        self.mood: str | None = None
        self._decided = not self.want_mood
        self._emitted = 0
        self.content = ""
        self.done = False
        self.retracted = False

    def _body(self) -> str | None:
        from wednesday.core.persona import parse_mood_prefix  # local: avoid import cycle
        raw = self.raw
        if "</think>" in raw:
            raw = raw.split("</think>")[-1]
        if not self._decided:
            mood, rest, decided = parse_mood_prefix(raw)
            if not decided:
                return None
            if mood:
                self.mood = mood
            return rest.lstrip()  # rest has any leading [tag] removed, known mood or not
        if self.want_mood:
            _m, rest, _ = parse_mood_prefix(raw)
            raw = rest
        return raw.lstrip()

    def feed(self, text: str) -> str:
        if self.done:
            return ""
        had_think = "</think>" in self.raw
        self.raw += text
        if "</think>" in self.raw and not had_think:
            if self._emitted:
                self.retracted = True
            self._emitted = 0
            self.content = ""
            self.mood = None
            self._decided = not self.want_mood  # the real reply (and its mood tag) starts now
        body = self._body()
        if body is None:
            return ""
        self._decided = True
        m = self._second.search(body)
        if m:
            body = body[: m.start()]
            self.done = True
            safe = body.rstrip()
        else:
            pm = self._partial.search(body)
            safe = body[: pm.start()] if pm else body
        new = safe[self._emitted:]
        self._emitted = len(safe)
        self.content = safe
        return new

    def finish(self) -> str:
        body = self._body()
        if body is None:  # stream ended while still deciding: show what we have
            body = self.raw.lstrip()
        m = self._second.search(body)
        if m:
            body = body[: m.start()]
        body = re.sub(r"\n\s*\[[A-Za-z]{0,12}$", "", body).rstrip()
        new = body[self._emitted:] if len(body) >= self._emitted else ""
        self._emitted = len(body)
        self.content = body
        return new
