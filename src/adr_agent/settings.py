from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_ADR_MARKER = "adr-agent"

_HOOKS_CONFIG = {
    "SessionStart": [
        {
            "hooks": [{"type": "command", "command": "adr-agent session-start"}],
        }
    ],
    "PreToolUse": [
        {
            "matcher": "Edit|Write|MultiEdit",
            "hooks": [{"type": "command", "command": "adr-agent pre-tool-use"}],
        }
    ],
    "PostToolUse": [
        {
            "matcher": "Edit|Write|MultiEdit",
            "hooks": [{"type": "command", "command": "adr-agent post-tool-use"}],
        }
    ],
    "SessionEnd": [
        {
            "hooks": [{"type": "command", "command": "adr-agent session-end"}],
        }
    ],
}


def _settings_path(project_root: Path) -> Path:
    return project_root / ".claude" / "settings.json"


def load_settings(project_root: Path) -> dict:
    path = _settings_path(project_root)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def save_settings(settings: dict, project_root: Path) -> None:
    path = _settings_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")


def add_adr_hooks(project_root: Path) -> None:
    settings = load_settings(project_root)
    hooks = settings.setdefault("hooks", {})

    for event, new_entries in _HOOKS_CONFIG.items():
        existing: list = hooks.setdefault(event, [])
        for entry in new_entries:
            cmd = entry["hooks"][0]["command"]
            # Idempotent: skip if already present
            if any(
                any(h.get("command") == cmd for h in e.get("hooks", []))
                for e in existing
            ):
                continue
            existing.append(entry)

    save_settings(settings, project_root)


def remove_adr_hooks(project_root: Path) -> None:
    settings = load_settings(project_root)
    hooks = settings.get("hooks", {})
    adr_commands = {h["hooks"][0]["command"] for entries in _HOOKS_CONFIG.values() for h in entries}

    for event in list(hooks.keys()):
        hooks[event] = [
            entry for entry in hooks[event]
            if not any(h.get("command") in adr_commands for h in entry.get("hooks", []))
        ]
        if not hooks[event]:
            del hooks[event]

    save_settings(settings, project_root)


def check_hooks_present(project_root: Path) -> dict[str, bool]:
    settings = load_settings(project_root)
    hooks = settings.get("hooks", {})
    result = {}
    for event, entries in _HOOKS_CONFIG.items():
        cmd = entries[0]["hooks"][0]["command"]
        existing = hooks.get(event, [])
        result[event] = any(
            any(h.get("command") == cmd for h in e.get("hooks", []))
            for e in existing
        )
    return result
