from wednesday.core import persona
from wednesday.core.text import ReplyFilter, SentenceChunker, for_speech, needs_thinking, needs_tools


def run(chunks, want_mood=True):
    f = ReplyFilter(persona.MOODS, want_mood)
    shown = ""
    for c in chunks:
        t = f.feed(c)
        if f.retracted:
            shown, f.retracted = "", False
        shown += t
        if f.done:
            break
    shown += f.finish()
    return f.mood, shown


def test_mood_tag_split_across_chunks():
    assert run(["[am", "used]", " \n\nHello", " there."]) == ("amused", "Hello there.")


def test_second_tag_cuts_rambling():
    assert run(["[calm] One.", "\n\n[conc", "erned] Two."]) == ("calm", "One.")


def test_invented_user_turn_cut():
    assert run(["[warm] Fine.\nUser: hi"]) == ("warm", "Fine.")


def test_think_leak_retracted():
    assert run(["pseudo thoughts", "</think>", "\n[calm] Real."]) == ("calm", "Real.")


def test_no_tag_passes_through():
    assert run(["Plain ", "text."]) == (None, "Plain text.")


def test_brackets_in_code_not_cut():
    assert run(["[curious] Use `a[i]`", "\n[1, 2] lists."])[1] == "Use `a[i]`\n[1, 2] lists."


def test_trailing_partial_tag_dropped():
    assert run(["[smug] Done.\n\n[concerned"]) == ("smug", "Done.")


def test_sentence_chunker_waits_for_code_fence():
    c = SentenceChunker(min_chars=5)
    assert c.feed("Here is code. ```py\nx = 1. y = 2.") == ["Here is code."]
    assert c.feed("\n``` Done now. Next") == ["```py\nx = 1. y = 2.\n``` Done now."]


def test_for_speech_strips_markdown():
    s = for_speech("**Bold** and `code`, see [docs](https://x.y).\n```\nrm -rf\n```")
    assert "rm -rf" not in s and "**" not in s and "docs" in s


def test_routers():
    assert not needs_tools("tell me a joke")
    assert needs_tools("read ~/notes.txt")
    assert needs_tools("what's the latest llama.cpp release?")
    assert not needs_thinking("what's 2+2?")
    assert needs_thinking("why does my algorithm have quadratic complexity?")


def test_quips_respect_humor_setting():
    assert persona.quip("idle", humor=0.1) is None
    assert persona.quip("error", humor=0.1)  # important notices are never silenced


def test_parse_mood_prefix_waits():
    assert persona.parse_mood_prefix("[cur")[2] is False
    assert persona.parse_mood_prefix("[curious] hi")[:2] == ("curious", "hi")


def test_unknown_mood_tag_still_stripped():
    assert run(["[jackpot] Jazzy."]) == (None, "Jazzy.")


def test_router_needs_action_not_topic():
    assert not needs_tools("explain a ROS 2 topic in one sentence")
    assert not needs_tools("which ROS distro do I use?")
    assert needs_tools("check the git status of my repo")
    assert needs_tools("run the tests in ~/ros2_ws")
    assert needs_tools("what's the latest news about llama.cpp?")
    assert needs_tools("what's my GPU temperature?")
