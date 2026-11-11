# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: Wire MemHub into each CLI host: the memhub MCP server, a recall
skill, and (where the host has hooks) transcript capture / prompt-time recall.

Every host file is edited in place, keeping all other entries; the first edit
leaves ``<file>.memhub-hub.bak``. YAML hosts are rewritten round-trip, so the
user's comments, quoting and indentation survive.
"""
from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ruamel.yaml import YAML

from memhub.hub.config import hub_root

MCP_NAME = "memhub"
SKILL_NAME = "memhub-recall"
SKILL_MARKER = "<!-- managed by memhub hub -->"
AGENTICA_CONSUMER = "memhub"
TOML_BEGIN = "# >>> memhub hub >>>"
TOML_END = "# <<< memhub hub <<<"
HOOKS_BEGIN = "# >>> memhub hub hooks >>>"
HOOKS_END = "# <<< memhub hub hooks <<<"
# Trae keeps its MCP servers beside the editor's user settings.
TRAE_MCP = (
    "Library/Application Support/Trae/User/mcp.json"
    if sys.platform == "darwin"
    else ".config/Trae/User/mcp.json"
)
# Our hook commands always run this package, which is how we find them again
# (the prefix also matches hooks written as ``-m memhub.cli.main``).
HOOK_SIGNATURE = "-m memhub.cli"
# agentica.shell_hooks.config.SHELL_HOOK_EVENTS; unlisted events default to on there.
AGENTICA_EVENTS = (
    "run.started",
    "run.completed",
    "run.failed",
    "run.cancelled",
    "tool.started",
    "tool.completed",
    "session.started",
    "session.ended",
    "needs.approval",
    "needs.input",
    "needs.resolved",
    "UserPromptSubmit",
)
# One consumer, one command: ``hub hook`` answers UserPromptSubmit with recalled
# memories (beside agentica's own recall, never replacing it) and captures the
# transcript on the rest.
AGENTICA_HOOK_EVENTS = ("UserPromptSubmit", "run.completed", "session.ended")


@dataclass(frozen=True)
class Launch:
    """Host commands use this interpreter and the Core on this machine."""

    host: str
    hub_dir: Path

    def memhub(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "memhub.cli", *args]

    def mcp(self) -> dict[str, Any]:
        return {
            "command": sys.executable,
            "args": [
                "-m",
                "memhub.hub.mcp",
                "--hub-dir",
                str(self.hub_dir),
            ],
        }

    def hook(self) -> list[str]:
        return self.memhub("hook", self.host, "--hub-dir", str(self.hub_dir))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".memhub-hub.bak")
    if path.is_file() and not backup.exists():
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".memhub-hub.tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.is_file():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8") or "{}") if path.is_file() else {}


def _dump_json(path: Path, data: dict) -> None:
    _write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _round_trip_yaml() -> YAML:
    rt = YAML()
    rt.preserve_quotes = True
    rt.width = 4096
    rt.indent(mapping=2, sequence=4, offset=2)
    return rt


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    return _round_trip_yaml().load(path.read_text(encoding="utf-8")) or {}


def _dump_yaml(path: Path, data: dict) -> None:
    buf = io.StringIO()
    _round_trip_yaml().dump(data, buf)
    _write(path, buf.getvalue())


class Part(Protocol):
    def install(self, path: Path, launch: Launch) -> None: ...
    def uninstall(self, path: Path) -> bool: ...
    def installed(self, path: Path) -> bool: ...


class JsonMcp:
    """``{"mcpServers": {...}}`` — Agentica, Claude Code, Cline, CodeBuddy, Copilot,
    Cursor, Kimi, Kiro, Pi, Qoder, Trae, WorkBuddy."""

    def install(self, path: Path, launch: Launch) -> None:
        data = _load_json(path)
        data.setdefault("mcpServers", {})[MCP_NAME] = launch.mcp()
        _dump_json(path, data)

    def uninstall(self, path: Path) -> bool:
        data = _load_json(path)
        if MCP_NAME not in data.get("mcpServers", {}):
            return False
        del data["mcpServers"][MCP_NAME]
        _dump_json(path, data)
        return True

    def installed(self, path: Path) -> bool:
        return MCP_NAME in _load_json(path).get("mcpServers", {})


class ZCodeMcp:
    """ZCode ``~/.zcode/cli/config.json`` ``mcp.servers``: ``{"type": "stdio", "command": ..., "args": [...]}``."""

    def install(self, path: Path, launch: Launch) -> None:
        data = _load_json(path)
        mcp = launch.mcp()
        data.setdefault("mcp", {}).setdefault("servers", {})[MCP_NAME] = {
            "type": "stdio",
            "command": mcp["command"],
            "args": mcp["args"],
        }
        _dump_json(path, data)

    def uninstall(self, path: Path) -> bool:
        data = _load_json(path)
        servers = (data.get("mcp") or {}).get("servers") or {}
        if MCP_NAME not in servers:
            return False
        del servers[MCP_NAME]
        _dump_json(path, data)
        return True

    def installed(self, path: Path) -> bool:
        return MCP_NAME in ((_load_json(path).get("mcp") or {}).get("servers") or {})


class OpenCodeMcp:
    """OpenCode ``opencode.json`` ``mcp``: ``{"type": "local", "command": [...]}``."""

    def install(self, path: Path, launch: Launch) -> None:
        data = _load_json(path)
        mcp = launch.mcp()
        data.setdefault("mcp", {})[MCP_NAME] = {
            "type": "local",
            "command": [mcp["command"], *mcp["args"]],
        }
        _dump_json(path, data)

    def uninstall(self, path: Path) -> bool:
        data = _load_json(path)
        if MCP_NAME not in data.get("mcp", {}):
            return False
        del data["mcp"][MCP_NAME]
        _dump_json(path, data)
        return True

    def installed(self, path: Path) -> bool:
        return MCP_NAME in _load_json(path).get("mcp", {})


class YamlMcp:
    """Hermes ``config.yaml`` ``mcp_servers``."""

    def install(self, path: Path, launch: Launch) -> None:
        data = _load_yaml(path)
        data.setdefault("mcp_servers", {})[MCP_NAME] = launch.mcp()
        _dump_yaml(path, data)

    def uninstall(self, path: Path) -> bool:
        data = _load_yaml(path)
        if MCP_NAME not in (data.get("mcp_servers") or {}):
            return False
        del data["mcp_servers"][MCP_NAME]
        _dump_yaml(path, data)
        return True

    def installed(self, path: Path) -> bool:
        return MCP_NAME in (_load_yaml(path).get("mcp_servers") or {})


class TomlMcp:
    """Codex ``config.toml``: a marked block, so the rest of the file stays byte-identical."""

    _block = re.compile(rf"\n?{re.escape(TOML_BEGIN)}.*?{re.escape(TOML_END)}\n", re.DOTALL)

    def install(self, path: Path, launch: Launch) -> None:
        text = self._block.sub("", path.read_text(encoding="utf-8")) if path.is_file() else ""
        mcp = launch.mcp()
        block = (
            f"{TOML_BEGIN}\n[mcp_servers.{MCP_NAME}]\n"
            f"command = {json.dumps(mcp['command'])}\n"
            f"args = {json.dumps(mcp['args'])}\n{TOML_END}\n"
        )
        sep = "\n" if text and not text.endswith("\n") else ""
        _write(path, f"{text}{sep}\n{block}" if text else block)

    def uninstall(self, path: Path) -> bool:
        if not self.installed(path):
            return False
        _write(path, self._block.sub("", path.read_text(encoding="utf-8")))
        return True

    def installed(self, path: Path) -> bool:
        return path.is_file() and TOML_BEGIN in path.read_text(encoding="utf-8")


class AgenticaHooks:
    """``settings.hooks.consumers`` in ~/.agentica/config.yaml: one consumer that
    recalls on ``UserPromptSubmit`` and captures on run end.

    Every event is listed: agentica turns an unlisted one on. Ours are found by
    ``HOOK_SIGNATURE`` in the command, so a consumer from an older install is
    replaced whatever it was named.
    """

    @staticmethod
    def _ours(consumer: dict) -> bool:
        return HOOK_SIGNATURE in " ".join(consumer.get("command") or [])

    def install(self, path: Path, launch: Launch) -> None:
        data = _load_yaml(path)
        hooks = data.setdefault("settings", {}).setdefault("hooks", {})
        hooks["enabled"] = True
        consumers = [c for c in hooks.get("consumers") or [] if not self._ours(c)]
        consumers.append(
            {
                "name": AGENTICA_CONSUMER,
                "command": launch.hook(),
                "events": {e: e in AGENTICA_HOOK_EVENTS for e in AGENTICA_EVENTS},
            }
        )
        hooks["consumers"] = consumers
        _dump_yaml(path, data)

    def uninstall(self, path: Path) -> bool:
        data = _load_yaml(path)
        hooks = (data.get("settings") or {}).get("hooks") or {}
        consumers = hooks.get("consumers") or []
        kept = [c for c in consumers if not self._ours(c)]
        if len(kept) == len(consumers):
            return False
        hooks["consumers"] = kept
        _dump_yaml(path, data)
        return True

    def installed(self, path: Path) -> bool:
        hooks = (_load_yaml(path).get("settings") or {}).get("hooks") or {}
        return any(self._ours(c) for c in hooks.get("consumers") or [])


class ClaudeStyleHooks:
    """Claude Code ``settings.json`` / Codex ``hooks.json`` / Qoder ``settings.json``:
    recall on prompt, capture on Stop."""

    @staticmethod
    def _ours(entry: dict) -> bool:
        return any(HOOK_SIGNATURE in h.get("command", "") for h in entry.get("hooks", []))

    def install(self, path: Path, launch: Launch) -> None:
        data = _load_json(path)
        hooks = data.setdefault("hooks", {})
        command = shlex.join(launch.hook())
        for event in ("UserPromptSubmit", "Stop"):
            entries = [e for e in hooks.get(event, []) if not self._ours(e)]
            entries.append({"hooks": [{"type": "command", "command": command}]})
            hooks[event] = entries
        _dump_json(path, data)

    def uninstall(self, path: Path) -> bool:
        data = _load_json(path)
        hooks = data.get("hooks", {})
        changed = False
        for event, entries in list(hooks.items()):
            kept = [e for e in entries if not self._ours(e)]
            if len(kept) != len(entries):
                hooks[event] = kept
                changed = True
        if changed:
            _dump_json(path, data)
        return changed

    def installed(self, path: Path) -> bool:
        hooks = _load_json(path).get("hooks", {})
        return any(self._ours(e) for entries in hooks.values() for e in entries)


class ZCodeHooks:
    """ZCode ``~/.zcode/cli/config.json`` ``hooks.events``: same entry shape as Claude Code,
    under ``hooks: {"enabled": True, "events": {...}}``."""

    def install(self, path: Path, launch: Launch) -> None:
        data = _load_json(path)
        hooks = data.setdefault("hooks", {})
        hooks["enabled"] = True
        events = hooks.setdefault("events", {})
        command = shlex.join(launch.hook())
        for event in ("UserPromptSubmit", "Stop"):
            entries = [e for e in events.get(event, []) if not ClaudeStyleHooks._ours(e)]
            entries.append({"hooks": [{"type": "command", "command": command}]})
            events[event] = entries
        _dump_json(path, data)

    def uninstall(self, path: Path) -> bool:
        data = _load_json(path)
        events = (data.get("hooks") or {}).get("events") or {}
        changed = False
        for event, entries in list(events.items()):
            kept = [e for e in entries if not ClaudeStyleHooks._ours(e)]
            if len(kept) != len(entries):
                events[event] = kept
                changed = True
        if changed:
            _dump_json(path, data)
        return changed

    def installed(self, path: Path) -> bool:
        events = (_load_json(path).get("hooks") or {}).get("events") or {}
        return any(ClaudeStyleHooks._ours(e) for entries in events.values() for e in entries)


class CopilotHooks:
    """GitHub Copilot ``~/.copilot/hooks/memhub.json``: one file owned by memhub,
    with ``UserPromptSubmit`` and ``Stop`` command entries."""

    def install(self, path: Path, launch: Launch) -> None:
        command = shlex.join(launch.hook())
        entry = {"type": "command", "command": command, "timeoutSeconds": 30}
        _dump_json(
            path,
            {
                "version": 1,
                "hooks": {"UserPromptSubmit": [entry], "Stop": [entry]},
            },
        )

    def uninstall(self, path: Path) -> bool:
        if not self.installed(path):
            return False
        path.unlink()
        return True

    def installed(self, path: Path) -> bool:
        return path.is_file() and HOOK_SIGNATURE in path.read_text(encoding="utf-8")


class KiroHooks:
    """Kiro ``~/.kiro/hooks/memhub.json``: v1 standalone hook file with
    ``UserPromptSubmit`` and ``Stop`` triggers."""

    def install(self, path: Path, launch: Launch) -> None:
        command = shlex.join(launch.hook())
        action = {"type": "command", "command": command}
        _dump_json(
            path,
            {
                "version": "v1",
                "hooks": [
                    {"name": "memhub-recall", "trigger": "UserPromptSubmit", "action": action},
                    {"name": "memhub-capture", "trigger": "Stop", "action": action},
                ],
            },
        )

    def uninstall(self, path: Path) -> bool:
        if not self.installed(path):
            return False
        path.unlink()
        return True

    def installed(self, path: Path) -> bool:
        return path.is_file() and HOOK_SIGNATURE in path.read_text(encoding="utf-8")


class OpenCodePlugin:
    """OpenCode auto-loaded plugin bridging its events onto ``hub hook opencode``:
    ``chat.message`` → ``UserPromptSubmit`` (recall), ``session.idle`` → ``Stop`` (capture)."""

    def install(self, path: Path, launch: Launch) -> None:
        hook = json.dumps(launch.hook(), ensure_ascii=False)
        script = f"""// {SKILL_MARKER}
import {{ spawn }} from "child_process";

const HOOK = {hook};

function run(payload) {{
  return new Promise((resolve) => {{
    try {{
      const child = spawn(HOOK[0], HOOK.slice(1), {{ stdio: ["pipe", "pipe", "ignore"] }});
      let out = "";
      child.stdout.on("data", (chunk) => {{ out += chunk.toString(); }});
      child.on("close", () => resolve(out));
      child.on("error", () => resolve(""));
      child.stdin.end(JSON.stringify(payload));
    }} catch {{
      resolve("");
    }}
  }});
}}

export default async () => ({{
  "chat.message": async (input, output) => {{
    const prompt = output.parts
      .filter((p) => p.type === "text" && !p.synthetic && p.text)
      .map((p) => p.text)
      .join("\\n")
      .trim();
    if (!prompt) return;
    const raw = await run({{ hook_event_name: "UserPromptSubmit", session_id: input.sessionID, prompt }});
    if (!raw) return;
    const ctx = JSON.parse(raw).hookSpecificOutput?.additionalContext;
    if (ctx) output.message.system = output.message.system ? `${{output.message.system}}\\n\\n${{ctx}}` : ctx;
  }},
  event: async ({{ event }}) => {{
    if (event.type === "session.idle") {{
      await run({{ hook_event_name: "Stop", session_id: event.properties.sessionID }});
    }}
  }},
}});
"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(script, encoding="utf-8")

    def uninstall(self, path: Path) -> bool:
        if not self.installed(path):
            return False
        path.unlink()
        return True

    def installed(self, path: Path) -> bool:
        return path.is_file() and SKILL_MARKER in path.read_text(encoding="utf-8")


class CursorHooks:
    """``~/.cursor/hooks.json``: recall on ``beforeSubmitPrompt``, capture on ``stop``."""

    @staticmethod
    def _ours(entry: dict) -> bool:
        return HOOK_SIGNATURE in entry.get("command", "")

    def install(self, path: Path, launch: Launch) -> None:
        data = _load_json(path)
        data.setdefault("version", 1)
        hooks = data.setdefault("hooks", {})
        command = shlex.join(launch.hook())
        for event in ("beforeSubmitPrompt", "stop"):
            entries = [e for e in hooks.get(event, []) if not self._ours(e)]
            entries.append({"command": command})
            hooks[event] = entries
        _dump_json(path, data)

    def uninstall(self, path: Path) -> bool:
        data = _load_json(path)
        hooks = data.get("hooks", {})
        changed = False
        for event, entries in list(hooks.items()):
            kept = [e for e in entries if not self._ours(e)]
            if len(kept) != len(entries):
                hooks[event] = kept
                changed = True
        if changed:
            _dump_json(path, data)
        return changed

    def installed(self, path: Path) -> bool:
        hooks = _load_json(path).get("hooks", {})
        return any(self._ours(e) for entries in hooks.values() for e in entries)


@dataclass(frozen=True)
class TomlHooks:
    """Kimi ``config.toml`` ``[[hooks]]``: a marked block at the end of the file, so
    the rest of it (providers, other tools' hooks) stays byte-identical.

    ``UserPromptSubmit`` recalls and ``Stop`` captures; ``Stop`` must exit 0, since
    a blocking Stop hook makes Kimi run another turn. ``events`` is per product:
    kimi-cli (1.49) ignores what a prompt hook prints, so recalling there is wasted
    work and it only captures.
    """

    events: tuple[str, ...]

    _block = re.compile(
        rf"\n?{re.escape(HOOKS_BEGIN)}.*?{re.escape(HOOKS_END)}\n", re.DOTALL
    )

    def install(self, path: Path, launch: Launch) -> None:
        text = self._block.sub("", path.read_text(encoding="utf-8")) if path.is_file() else ""
        command = json.dumps(shlex.join(launch.hook()))
        entries = "\n".join(
            f'[[hooks]]\nevent = "{event}"\ncommand = {command}\ntimeout = 45\n'
            for event in self.events
        )
        block = f"{HOOKS_BEGIN}\n{entries}{HOOKS_END}\n"
        sep = "\n" if text and not text.endswith("\n") else ""
        _write(path, f"{text}{sep}\n{block}" if text else block)

    def uninstall(self, path: Path) -> bool:
        if not self.installed(path):
            return False
        _write(path, self._block.sub("", path.read_text(encoding="utf-8")))
        return True

    def installed(self, path: Path) -> bool:
        return path.is_file() and HOOKS_BEGIN in path.read_text(encoding="utf-8")


def _patch_yaml() -> YAML:
    """dsh patch files are a top-level list written ``- id: ...`` with no extra indent."""
    rt = YAML()
    rt.preserve_quotes = True
    rt.width = 4096
    rt.indent(mapping=2, sequence=2, offset=0)
    # dsh writes ``key: null``; ruamel would turn that into an empty ``key:``.
    rt.representer.add_representer(
        type(None), lambda self, _data: self.represent_scalar("tag:yaml.org,2002:null", "null")
    )
    return rt


class DshMcp:
    """DeepSeek Harness (app and CLI): ``mcp-manager`` servers in each profile's
    ``cordis.patch.yml`` (``~/.dsh/profiles/<name>/``).

    A profile is created by dsh itself on first boot, with a ``package.json`` the
    patch depends on, so only profiles that already exist are edited.
    """

    ENTRY_ID = "mcp-manager"
    ENTRY_NAME = "@deepseek-ai/dsh-mcp-manager"

    @staticmethod
    def _patches(root: Path) -> list[Path]:
        return sorted(root.glob("*/cordis.patch.yml")) if root.is_dir() else []

    @staticmethod
    def _load(path: Path) -> list:
        return _patch_yaml().load(path.read_text(encoding="utf-8")) or []

    @staticmethod
    def _dump(path: Path, data: list) -> None:
        buf = io.StringIO()
        _patch_yaml().dump(data, buf)
        _write(path, buf.getvalue())

    def install(self, path: Path, launch: Launch) -> None:
        mcp = launch.mcp()
        server = {"transport": "stdio", "command": mcp["command"], "args": mcp["args"]}
        for patch in self._patches(path):
            data = self._load(patch)
            entry = next((e for e in data if e.get("id") == self.ENTRY_ID), None)
            if entry is None:
                entry = {"id": self.ENTRY_ID, "name": self.ENTRY_NAME}
                data.append(entry)
            entry.setdefault("config", {}).setdefault("servers", {})[MCP_NAME] = server
            self._dump(patch, data)

    def uninstall(self, path: Path) -> bool:
        changed = False
        for patch in self._patches(path):
            data = self._load(patch)
            for entry in list(data):
                servers = (entry.get("config") or {}).get("servers") or {}
                if entry.get("id") != self.ENTRY_ID or MCP_NAME not in servers:
                    continue
                del servers[MCP_NAME]
                if not servers:
                    del entry["config"]["servers"]
                if not entry["config"]:
                    del entry["config"]
                if set(entry) <= {"id", "name"}:
                    data.remove(entry)
                changed = True
            self._dump(patch, data)
        return changed

    def installed(self, path: Path) -> bool:
        patches = self._patches(path)
        return bool(patches) and all(
            MCP_NAME
            in next(
                (
                    (e.get("config") or {}).get("servers") or {}
                    for e in self._load(patch)
                    if e.get("id") == self.ENTRY_ID
                ),
                {},
            )
            for patch in patches
        )


@dataclass(frozen=True)
class HostSpec:
    # Relative to $HOME; the skill lives at <home_dir>/skills/<SKILL_NAME>/SKILL.md.
    home_dir: str
    # None for a host that speaks no MCP (OpenClaw).
    mcp: tuple[Part, str] | None = None
    hooks: tuple[Part, str] | None = None
    binaries: tuple[str, ...] = ()


HOSTS: dict[str, HostSpec] = {
    "agentica": HostSpec(
        ".agentica",
        (JsonMcp(), ".agentica/mcp_config.json"),
        (AgenticaHooks(), ".agentica/config.yaml"),
        ("agentica",),
    ),
    "claude-code": HostSpec(
        ".claude",
        (JsonMcp(), ".claude.json"),
        (ClaudeStyleHooks(), ".claude/settings.json"),
        ("claude",),
    ),
    # Cline: global MCP in ~/.cline/data/settings/cline_mcp_settings.json, skills in ~/.cline/skills;
    # sessions are scanned from ~/.cline/data/sessions and legacy VS Code tasks.
    "cline": HostSpec(
        ".cline",
        (JsonMcp(), ".cline/data/settings/cline_mcp_settings.json"),
        None,
        ("cline",),
    ),
    "codebuddy": HostSpec(
        ".codebuddy",
        (JsonMcp(), ".codebuddy/mcp.json"),
        (ClaudeStyleHooks(), ".codebuddy/settings.json"),
        ("codebuddy",),
    ),
    # Codex runs hooks only with `[features] hooks = true` and after the user trusts them.
    "codex": HostSpec(
        ".codex",
        (TomlMcp(), ".codex/config.toml"),
        (ClaudeStyleHooks(), ".codex/hooks.json"),
        ("codex",),
    ),
    "copilot": HostSpec(
        ".copilot",
        (JsonMcp(), ".copilot/mcp-config.json"),
        (CopilotHooks(), ".copilot/hooks/memhub.json"),
        ("copilot",),
    ),
    "cursor": HostSpec(
        ".cursor",
        (JsonMcp(), ".cursor/mcp.json"),
        (CursorHooks(), ".cursor/hooks.json"),
        ("cursor",),
    ),
    # DeepSeek Harness (app and CLI share ~/.dsh): MCP in every profile patch, skill
    # in ~/.dsh/skills. The desktop bundle has no hooks plugin, so sessions are scanned.
    "dsh": HostSpec(".dsh", (DshMcp(), ".dsh/profiles"), None, ("dsh",)),
    # Kimi: ``mcp.json`` for servers, ``config.toml`` for hooks. kimi-cli is the
    # Python CLI (~/.kimi), which injects nothing a prompt hook prints; kimi-code
    # (~/.kimi-code) is its successor and does.
    "kimi-cli": HostSpec(
        ".kimi",
        (JsonMcp(), ".kimi/mcp.json"),
        (TomlHooks(("Stop",)), ".kimi/config.toml"),
        ("kimi",),
    ),
    "kimi-code": HostSpec(
        ".kimi-code",
        (JsonMcp(), ".kimi-code/mcp.json"),
        (TomlHooks(("UserPromptSubmit", "Stop")), ".kimi-code/config.toml"),
        ("kimi-code",),
    ),
    "kiro": HostSpec(
        ".kiro",
        (JsonMcp(), ".kiro/settings/mcp.json"),
        (KiroHooks(), ".kiro/hooks/memhub.json"),
        ("kiro", "kiro-cli"),
    ),
    # Hermes is scanned from its state.db; it gets MCP + skill, no hooks.
    "hermes": HostSpec(".hermes", (YamlMcp(), ".hermes/config.yaml"), None, ("hermes",)),
    # OpenClaw: zero-JS light integration — skill in ~/.openclaw/skills (uses CLI table)
    # and sessions scanned from ~/.openclaw/agents/*/sessions/*.jsonl.
    "openclaw": HostSpec(".openclaw", None, None, ("openclaw",)),
    "opencode": HostSpec(
        ".config/opencode",
        (OpenCodeMcp(), ".config/opencode/opencode.json"),
        (OpenCodePlugin(), ".config/opencode/plugins/memhub-hub.js"),
        ("opencode",),
    ),
    # Pi: zero-JS light integration — MCP in ~/.pi/agent/mcp.json, skill in
    # ~/.pi/agent/skills, sessions scanned from ~/.pi/agent/sessions.
    "pi": HostSpec(".pi/agent", (JsonMcp(), ".pi/agent/mcp.json"), None, ("pi",)),
    # Qoder (CLI and IDE share ~/.qoder): Claude-style settings.json for mcpServers + hooks.
    "qoder": HostSpec(
        ".qoder",
        (JsonMcp(), ".qoder/settings.json"),
        (ClaudeStyleHooks(), ".qoder/settings.json"),
        ("qoder",),
    ),
    # Trae reads Claude-style hooks from ~/.trae/hooks.json; its payload carries the
    # prompt and the final answer, so no transcript is read.
    "trae": HostSpec(
        ".trae",
        (JsonMcp(), TRAE_MCP),
        (ClaudeStyleHooks(), ".trae/hooks.json"),
        ("trae",),
    ),
    "workbuddy": HostSpec(
        ".workbuddy",
        (JsonMcp(), ".workbuddy/mcp.json"),
        (ClaudeStyleHooks(), ".workbuddy/settings.json"),
        ("workbuddy",),
    ),
    "zcode": HostSpec(
        ".zcode",
        (ZCodeMcp(), ".zcode/cli/config.json"),
        (ZCodeHooks(), ".zcode/cli/config.json"),
        ("zcode",),
    ),
}


# The installed skill; ``{host}`` and one placeholder per CLI command are filled in.
SKILL_TEMPLATE = Path(__file__).resolve().parent.parent / "skills" / SKILL_NAME / "SKILL.md"
CLI_COMMANDS = (
    "recall",
    "remember",
    "memory-get",
    "memory-list",
    "memory-update",
    "memory-delete",
    "status",
)


def render_skill(
    *, host: str, cli_prefix: list[str], commands: dict[str, list[str]]
) -> str:
    fields: dict[str, str] = {"host": host, "cli": shlex.join(cli_prefix)}
    fields.update(
        {name.replace("-", "_"): shlex.join(argv) for name, argv in commands.items()}
    )
    return SKILL_TEMPLATE.read_text(encoding="utf-8").format(**fields)


def _paths(host: str, home: Path) -> tuple[HostSpec, Path]:
    spec = HOSTS[host]
    return spec, home / spec.home_dir / "skills" / SKILL_NAME / "SKILL.md"


def install_host(
    host: str, *, hub_dir: str | Path | None = None, home: Path | None = None
) -> dict[str, Any]:
    home = home or Path.home()
    spec, skill = _paths(host, home)
    launch = Launch(host, hub_root(hub_dir))
    skill.parent.mkdir(parents=True, exist_ok=True)
    commands = {
        name: launch.memhub(name, "--hub-dir", str(launch.hub_dir))
        for name in CLI_COMMANDS
    }
    skill.write_text(
        render_skill(
            host=host,
            cli_prefix=launch.memhub(),
            commands=commands,
        ),
        encoding="utf-8",
    )
    written = [str(skill)]
    for part, rel in (p for p in (spec.mcp, spec.hooks) if p):
        part.install(home / rel, launch)
        if str(home / rel) not in written:
            written.append(str(home / rel))
    return {"host": host, "files": written}


def uninstall_host(host: str, *, home: Path | None = None) -> dict[str, Any]:
    home = home or Path.home()
    spec, skill = _paths(host, home)
    skill_removed = skill.is_file() and SKILL_MARKER in skill.read_text(encoding="utf-8")
    if skill_removed:
        shutil.rmtree(skill.parent)
    return {
        "host": host,
        "skill": skill_removed,
        "mcp": spec.mcp[0].uninstall(home / spec.mcp[1]) if spec.mcp else False,
        "hooks": spec.hooks[0].uninstall(home / spec.hooks[1]) if spec.hooks else False,
    }


def is_host_detected(
    host: str, *, home: Path | None = None, path_env: str | None = None
) -> bool:
    """True when the host's config directory or CLI binary exists on this machine."""
    home = home or Path.home()
    spec = HOSTS[host]
    if (home / spec.home_dir).exists():
        return True
    if host == "claude-code" and (home / ".claude.json").is_file():
        return True
    if host == "trae" and (home / TRAE_MCP).parent.exists():
        return True
    search_path = os.environ.get("PATH", "") if path_env is None else path_env
    if search_path and any(shutil.which(b, path=search_path) is not None for b in spec.binaries):
        return True
    return False


def detect_installed_hosts(
    *, home: Path | None = None, path_env: str | None = None
) -> list[str]:
    """Host ids detected on this machine (config directory or executable on PATH)."""
    return [
        host
        for host in HOSTS
        if is_host_detected(host, home=home, path_env=path_env)
    ]


def hosts_status(*, home: Path | None = None) -> dict[str, dict[str, Any]]:
    """Per host: skill / mcp / hooks installed (``None`` = the host has no such
    entry). A host whose own config file cannot be parsed (a user's hand edit left
    a trailing comma) reports it as ``error`` instead of failing every other host."""
    home = home or Path.home()
    out: dict[str, dict[str, Any]] = {}
    for host, spec in HOSTS.items():
        _, skill = _paths(host, home)
        try:
            out[host] = {
                "skill": skill.is_file()
                and SKILL_MARKER in skill.read_text(encoding="utf-8"),
                "mcp": spec.mcp[0].installed(home / spec.mcp[1]) if spec.mcp else None,
                "hooks": (
                    spec.hooks[0].installed(home / spec.hooks[1]) if spec.hooks else None
                ),
            }
        except Exception as exc:  # JSON / TOML / YAML errors of a third-party file
            out[host] = {
                "skill": False,
                "mcp": False,
                "hooks": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return out
