"""Permission engine: the only place that decides whether a tool action may run.

The model only proposes structured tool calls. Every call passes through `Permissions` here,
in code, before anything touches the filesystem, a process or the network. Nothing the model
says can change these rules (the config files themselves are on the deny list).

Decisions:
  READ     – run now
  APPROVAL – ask the human, with an explanation of the effect
  BLOCKED  – refuse, with a reason the model can relay
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import shlex
import socket
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse


NAT64 = ipaddress.ip_network("64:ff9b::/96")


def is_public_ip(raw: str) -> bool:
    """True for globally routable addresses. NAT64 (64:ff9b::/96, used on IPv6-only networks
    with DNS64) is judged by the IPv4 address embedded in its last 32 bits."""
    ip = ipaddress.ip_address(raw.split("%")[0])
    if ip.version == 6 and ip in NAT64:
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    elif ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class Level(str, Enum):
    READ = "read"
    APPROVAL = "approval"
    BLOCKED = "blocked"


@dataclass
class Decision:
    level: Level
    reason: str = ""
    destructive: bool = False

    @property
    def allowed(self) -> bool:
        return self.level == Level.READ


# Shell syntax we refuse to interpret. Commands are executed without a shell, so these would
# either be passed literally (confusing) or are attempts to chain hidden work behind a
# harmless-looking prefix. Pipes ARE supported: each stage is classified separately.
FORBIDDEN_TOKENS = {";", "&&", "||", "&", ">", ">>", "<", "<<", "<<<", "|&", ">&", "&>", "(", ")"}
FORBIDDEN_SUBSTRINGS = ("`", "$(", "${", "<(", ">(")

# Interpreters that execute arbitrary code given inline or from a file.
INTERPRETERS = {"bash", "sh", "zsh", "fish", "dash", "python", "python3", "perl", "ruby", "node",
                "lua", "php", "osascript", "pwsh", "xargs", "nohup", "timeout", "nice", "watch",
                "stdbuf", "setsid", "systemd-run", "flatpak-spawn", "busybox", "eval", "exec"}

# Arguments that turn a read-only tool into a writing/executing one.
DANGEROUS_ARGS = {
    "find": {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"},
    "sort": {"-o", "--output"},
    "tree": {"-o"},
    "rg": {"--pre"},
    "grep": set(),
}


class Permissions:
    def __init__(self, policy: dict, home: Path | None = None):
        self.home = Path(home or Path.home())
        self.policy = policy
        self.read_roots = [self._norm(p) for p in policy.get("read_roots", [])]
        self.write_roots = [self._norm(p) for p in policy.get("write_roots", [])]
        self.deny_paths = [self._norm(p) for p in policy.get("deny_paths", [])]
        self.deny_patterns = list(policy.get("deny_name_patterns", []))
        cmds = policy.get("commands", {})
        self.auto_allow = set(cmds.get("auto_allow", []))
        self.readonly_sub = {k: [s.split() for s in v] for k, v in cmds.get("readonly_subcommands", {}).items()}
        self.blocked = list(cmds.get("blocked", []))

    # ------------------------------------------------------------------ paths
    def _norm(self, p: str | Path) -> Path:
        s = str(p)
        if s == "~" or s.startswith("~/"):
            s = str(self.home) + s[1:]
        return Path(os.path.realpath(s))

    def resolve(self, p: str | Path, cwd: Path | None = None) -> Path:
        s = str(p)
        if s == "~" or s.startswith("~/"):
            s = str(self.home) + s[1:]
        path = Path(s)
        if not path.is_absolute():
            path = (cwd or self.home) / path
        # realpath resolves symlinks, so a link inside an allowed dir can't point us outside it.
        return Path(os.path.realpath(path))

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    def _denied(self, path: Path) -> str | None:
        for d in self.deny_paths:
            if self._within(path, d):
                return f"{path} is in a protected location ({d})"
        for part in path.parts:
            for pat in self.deny_patterns:
                if fnmatch.fnmatch(part.lower(), pat.lower()):
                    return f"'{part}' matches protected pattern '{pat}' (secrets/credentials)"
        return None

    def check_read(self, raw: str, cwd: Path | None = None) -> Decision:
        path = self.resolve(raw, cwd)
        if why := self._denied(path):
            return Decision(Level.BLOCKED, why)
        if not any(self._within(path, r) for r in self.read_roots):
            return Decision(Level.BLOCKED, f"{path} is outside the readable roots")
        return Decision(Level.READ)

    def check_write(self, raw: str, cwd: Path | None = None) -> Decision:
        path = self.resolve(raw, cwd)
        if why := self._denied(path):
            return Decision(Level.BLOCKED, why)
        if not any(self._within(path, r) for r in self.write_roots):
            roots = ", ".join(str(r) for r in self.write_roots) or "(none configured)"
            return Decision(Level.BLOCKED, f"{path} is outside the writable roots: {roots}")
        return Decision(Level.APPROVAL, f"write to {path}", destructive=path.exists())

    # ------------------------------------------------------------------ commands
    def split_pipeline(self, command: str) -> list[list[str]]:
        """Tokenise a command line into pipeline stages, or raise ValueError."""
        for bad in FORBIDDEN_SUBSTRINGS:
            if bad in command:
                raise ValueError(f"shell substitution '{bad}' is not supported")
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
        stages: list[list[str]] = [[]]
        for tok in tokens:
            if tok == "|":
                stages.append([])
            elif tok in FORBIDDEN_TOKENS or (tok and set(tok) <= set(";&|<>()")):
                raise ValueError(
                    f"'{tok}' is not supported: run one command at a time (pipes with | are fine)")
            else:
                stages[-1].append(tok)
        if any(not s for s in stages):
            raise ValueError("empty command in pipeline")
        return stages

    def _is_blocked_name(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in self.blocked)

    def _readonly_sub(self, prog: str, args: list[str]) -> bool:
        options = self.readonly_sub.get(prog)
        if options is None:
            return True  # plain read-only tool (ls, cat, ...)
        for opt in options:
            if args[: len(opt)] == opt:
                # "git config --get x" ok, but "git branch -D x" is not read-only
                if prog == "git" and opt[0] in {"branch", "tag", "remote", "stash"} and len(args) > len(opt):
                    extra = args[len(opt):]
                    if any(a.startswith("-") and a not in {"-v", "-vv", "-a", "-r", "--list", "-l"} for a in extra):
                        return False
                    if opt[0] in {"branch", "tag", "stash"} and any(not a.startswith("-") for a in extra):
                        return False
                    if opt[0] == "remote" and any(not a.startswith("-") for a in extra):
                        return False
                return True
        return False

    def check_stage(self, argv: list[str], cwd: Path) -> Decision:
        prog = os.path.basename(argv[0])
        args = argv[1:]
        if self._is_blocked_name(prog):
            return Decision(Level.BLOCKED, f"'{prog}' is blocked by policy — run it yourself if you really need it")

        # Any argument that looks like a path must not touch protected locations.
        for a in args:
            candidate = a.split("=", 1)[1] if a.startswith("-") and "=" in a else a
            if candidate.startswith(("/", "~", ".")) or "/" in candidate:
                if why := self._denied(self.resolve(candidate, cwd)):
                    return Decision(Level.BLOCKED, why)

        destructive_words = {"rm", "rmdir", "mv", "truncate", "unlink", "chmod", "chown", "kill"}
        if prog in destructive_words:
            if prog in {"rm", "rmdir", "unlink", "truncate", "mv"}:
                targets = [a for a in args if not a.startswith("-")]
                recursive = any(a in {"-r", "-R", "-rf", "-fr", "--recursive"} or
                                (a.startswith("-") and not a.startswith("--") and "r" in a.lower()) for a in args)
                for t in targets:
                    p = self.resolve(t, cwd)
                    if not any(self._within(p, r) for r in self.write_roots):
                        return Decision(Level.BLOCKED,
                                        f"'{prog}' on {p} is outside the writable roots — refusing")
                    if recursive and any(p == r for r in self.write_roots):
                        return Decision(Level.BLOCKED, f"refusing to recursively delete a whole root ({p})")
            return Decision(Level.APPROVAL, f"'{prog}' modifies or deletes files/processes", destructive=True)

        if prog in INTERPRETERS and not (prog in self.auto_allow and self._readonly_sub(prog, args) and args):
            return Decision(Level.APPROVAL, f"'{prog}' runs arbitrary code")

        if prog in self.auto_allow:
            bad = DANGEROUS_ARGS.get(prog, set())
            if any(a in bad for a in args):
                return Decision(Level.APPROVAL, f"'{prog}' with these options can modify files or run programs",
                                destructive=prog == "find" and "-delete" in args)
            if not self._readonly_sub(prog, args):
                sub = " ".join(args[:2])
                destructive = prog == "git" and any(
                    w in args for w in ("reset", "clean", "push", "rebase", "checkout", "restore", "rm"))
                return Decision(Level.APPROVAL, f"'{prog} {sub}' is not a read-only operation", destructive=destructive)
            # Read-only commands may still only look inside readable roots.
            for a in args:
                if a.startswith(("/", "~")):
                    d = self.check_read(a, cwd)
                    if d.level == Level.BLOCKED:
                        return d
            return Decision(Level.READ)
        return Decision(Level.APPROVAL, f"'{prog}' is not on the read-only allow list")

    def check_command(self, command: str, cwd: str | None = None) -> tuple[Decision, list[list[str]]]:
        try:
            stages = self.split_pipeline(command)
        except ValueError as e:
            return Decision(Level.BLOCKED, str(e)), []
        wd = self.resolve(cwd or "~")
        d = self.check_read(str(wd))
        if d.level == Level.BLOCKED:
            return Decision(Level.BLOCKED, f"working directory not allowed: {d.reason}"), stages
        worst = Decision(Level.READ)
        order = {Level.READ: 0, Level.APPROVAL: 1, Level.BLOCKED: 2}
        reasons = []
        for argv in stages:
            d = self.check_stage(argv, wd)
            if d.reason:
                reasons.append(d.reason)
            if order[d.level] > order[worst.level]:
                worst = Decision(d.level, d.reason, worst.destructive or d.destructive)
            else:
                worst.destructive = worst.destructive or d.destructive
        if worst.level != Level.READ:
            worst.reason = "; ".join(dict.fromkeys(reasons))
        return worst, stages

    # ------------------------------------------------------------------ network
    @staticmethod
    def check_url(url: str) -> Decision:
        """Only public http(s). Blocks localhost/LAN so web tools can't reach Wednesday's own
        API, the model server, your router, or other local services (SSRF)."""
        try:
            u = urlparse(url)
        except ValueError:
            return Decision(Level.BLOCKED, "malformed URL")
        if u.scheme not in ("http", "https") or not u.hostname:
            return Decision(Level.BLOCKED, "only http(s) URLs are allowed")
        if u.username or u.password:
            return Decision(Level.BLOCKED, "URLs with embedded credentials are not allowed")
        try:
            infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
        except socket.gaierror:
            return Decision(Level.BLOCKED, f"cannot resolve {u.hostname} (offline?)")
        for info in infos:
            if not is_public_ip(info[4][0]):
                return Decision(Level.BLOCKED, f"{u.hostname} resolves to a local/private address ({info[4][0]})")
        return Decision(Level.READ)
