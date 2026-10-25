"""The permission layer is the security boundary: these tests pin its behaviour."""
from wednesday.agent.permissions import Level, Permissions, is_public_ip


def lvl(p, cmd, cwd="~"):
    return p.check_command(cmd, cwd)[0].level


def test_read_allowed_inside_home(perms):
    p, home = perms
    assert p.check_read("~/notes.txt").level == Level.READ


def test_secrets_blocked(perms):
    p, home = perms
    assert p.check_read("~/.ssh/id_rsa").level == Level.BLOCKED
    assert p.check_read("~/proj/.env").level == Level.BLOCKED


def test_outside_roots_blocked(perms):
    p, _ = perms
    assert p.check_read("/etc/hostname").level == Level.BLOCKED


def test_symlink_escape_blocked(perms):
    p, home = perms
    (home / "proj" / "sneaky").symlink_to(home / ".ssh")
    assert p.check_read("~/proj/sneaky/id_rsa").level == Level.BLOCKED


def test_dotdot_escape_blocked(perms):
    p, _ = perms
    assert p.check_write("~/proj/../.ssh/authorized_keys").level == Level.BLOCKED


def test_write_requires_approval_inside_root(perms):
    p, _ = perms
    d = p.check_write("~/proj/new.py")
    assert d.level == Level.APPROVAL and not d.destructive


def test_write_outside_root_blocked(perms):
    p, _ = perms
    assert p.check_write("~/notes.txt").level == Level.BLOCKED


def test_harmless_commands_run(perms):
    p, _ = perms
    assert lvl(p, "ls -la") == Level.READ
    assert lvl(p, "git status") == Level.READ
    assert lvl(p, "git log --oneline | head -5") == Level.READ


def test_git_mutations_need_approval(perms):
    p, _ = perms
    assert lvl(p, "git push") == Level.APPROVAL
    assert lvl(p, "git branch -D main") == Level.APPROVAL
    d, _ = p.check_command("git reset --hard")
    assert d.level == Level.APPROVAL and d.destructive


def test_blocked_programs(perms):
    p, _ = perms
    for cmd in ("sudo ls", "dd if=/dev/zero of=/dev/sda", "mkfs.ext4 /dev/sda1", "shutdown now", "env"):
        assert lvl(p, cmd) == Level.BLOCKED, cmd


def test_shell_chaining_refused(perms):
    p, _ = perms
    for cmd in ("ls; rm -rf ~", "ls && sudo x", "echo hi > ~/notes.txt", "cat $(echo ~/.ssh/id_rsa)",
                "echo `whoami`", "ls || reboot", "sleep 1 &"):
        assert lvl(p, cmd) == Level.BLOCKED, cmd


def test_pipe_each_stage_checked(perms):
    p, _ = perms
    assert lvl(p, "echo x | sudo tee /etc/x") == Level.BLOCKED
    assert lvl(p, "cat ~/notes.txt | python3") == Level.APPROVAL


def test_rm_outside_write_roots_blocked(perms):
    p, _ = perms
    assert lvl(p, "rm -rf ~") == Level.BLOCKED
    assert lvl(p, "rm ~/notes.txt") == Level.BLOCKED
    assert lvl(p, "rm -rf ~/proj") == Level.BLOCKED  # whole root
    d, _ = p.check_command("rm ~/proj/old.txt")
    assert d.level == Level.APPROVAL and d.destructive


def test_secret_paths_in_args_blocked(perms):
    p, _ = perms
    assert lvl(p, "cat ~/.ssh/id_rsa") == Level.BLOCKED
    assert lvl(p, "grep -r KEY ~/proj/.env") == Level.BLOCKED


def test_find_exec_needs_approval(perms):
    p, _ = perms
    assert lvl(p, "find . -name '*.py'") == Level.READ
    d, _ = p.check_command("find . -delete")
    assert d.level == Level.APPROVAL and d.destructive


def test_interpreters_need_approval(perms):
    p, _ = perms
    assert lvl(p, "python3 --version") == Level.READ
    assert lvl(p, "python3 -c 'import os'") == Level.APPROVAL
    assert lvl(p, "bash -c 'ls'") == Level.APPROVAL


def test_unknown_program_needs_approval(perms):
    p, _ = perms
    assert lvl(p, "colcon build") == Level.APPROVAL


def test_url_checks():
    assert Permissions.check_url("file:///etc/passwd").level == Level.BLOCKED
    assert Permissions.check_url("http://127.0.0.1:7377/api/status").level == Level.BLOCKED
    assert Permissions.check_url("http://localhost:18181/").level == Level.BLOCKED
    assert Permissions.check_url("https://user:pw@example.com").level == Level.BLOCKED


def test_public_ip_classification():
    assert is_public_ip("8.8.8.8")
    assert is_public_ip("64:ff9b::14cc:f4c0")          # NAT64 of a public IPv4
    assert not is_public_ip("64:ff9b::7f00:1")         # NAT64 of 127.0.0.1
    assert not is_public_ip("192.168.1.10")
    assert not is_public_ip("::1")
    assert not is_public_ip("::ffff:10.0.0.1")


def test_command_idioms_normalized_but_still_checked():
    from wednesday.agent.tools import ToolExecutor
    n = ToolExecutor.normalize_command
    assert n("cd ~/proj && git status", None) == ("git status", "~/proj", False)
    assert n("ls -la 2>/dev/null", "~") == ("ls -la", "~", True)
    # only a single leading cd is translated; chains stay chains (and get blocked)
    assert n("cd a && ls && rm -rf ~", None)[0] == "cd a && ls && rm -rf ~"


def test_third_person_memories():
    from wednesday.core.orchestrator import third_person
    assert third_person("my main ROS workspace is ~/ros2_ws and I use Jazzy") == \
        "the user's main ROS workspace is ~/ros2_ws and the user uses Jazzy"
    assert third_person("I prefer tabs") == "the user prefers tabs"
