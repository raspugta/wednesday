import sqlite3

from wednesday.memory.store import MemoryStore


def test_add_search_delete(tmp_path):
    m = MemoryStore(tmp_path / "m.db")
    a = m.add("User prefers ROS 2 Jazzy with colcon builds", "preference")
    m.add("The robot arm uses a PCA9685 servo driver", "project", project="arm")
    hits = m.search("which ROS distro do I use?")
    assert hits and hits[0]["id"] == a
    assert m.delete(a)
    assert not m.search("Jazzy")


def test_fts_query_is_injection_safe(tmp_path):
    m = MemoryStore(tmp_path / "m.db")
    m.add("hello world")
    for evil in ['" OR 1=1 --', "NEAR(a b)", "a* AND b", "'; DROP TABLE memories; --"]:
        m.search(evil)  # must not raise
    assert m.count() == 1


def test_dedupe_and_retention(tmp_path):
    m = MemoryStore(tmp_path / "m.db")
    m.add("same", "note")
    m.add("same", "note")
    assert m.count() == 1
    m.add("keep me", "preference")
    m._db.execute("UPDATE memories SET updated_at = 0")
    assert m.apply_retention(30) == 1  # the old note goes, the preference stays
    assert [x["content"] for x in m.list()] == ["keep me"]


def test_survives_reopen(tmp_path):
    p = tmp_path / "m.db"
    m = MemoryStore(p)
    m.add("persist me", "fact")
    m.close()
    m2 = MemoryStore(p)
    assert m2.integrity_ok()
    assert m2.search("persist")[0]["content"] == "persist me"
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_tasks(tmp_path):
    m = MemoryStore(tmp_path / "m.db")
    t = m.create_task("Ship v1", ["write code", "test", "release"])
    assert len(t["steps"]) == 3
    for i in (1, 2, 3):
        t = m.update_step(t["id"], i, "done")
    assert t["status"] == "done"
