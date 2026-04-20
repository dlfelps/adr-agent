"""Tests for Claude settings.json hook management."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from adr_agent.settings import (
    add_adr_hooks,
    check_hooks_present,
    load_settings,
    remove_adr_hooks,
    save_settings,
)


def test_add_hooks_creates_settings(tmp_path: Path):
    add_adr_hooks(tmp_path)
    settings = load_settings(tmp_path)
    assert "hooks" in settings
    assert "SessionStart" in settings["hooks"]
    assert "PreToolUse" in settings["hooks"]
    assert "PostToolUse" in settings["hooks"]
    assert "SessionEnd" in settings["hooks"]


def test_add_hooks_idempotent(tmp_path: Path):
    add_adr_hooks(tmp_path)
    add_adr_hooks(tmp_path)
    settings = load_settings(tmp_path)
    # Should not duplicate entries
    for event, entries in settings["hooks"].items():
        commands = [h["command"] for e in entries for h in e.get("hooks", [])]
        assert len(commands) == len(set(commands)), f"Duplicate commands in {event}"


def test_add_hooks_merges_existing(tmp_path: Path):
    existing = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-tool pre"}]}
            ]
        }
    }
    save_settings(existing, tmp_path)
    add_adr_hooks(tmp_path)

    settings = load_settings(tmp_path)
    pre_hooks = settings["hooks"]["PreToolUse"]
    commands = [h["command"] for e in pre_hooks for h in e.get("hooks", [])]
    assert "my-tool pre" in commands
    assert "adr-agent pre-tool-use" in commands


def test_remove_hooks(tmp_path: Path):
    add_adr_hooks(tmp_path)
    remove_adr_hooks(tmp_path)
    settings = load_settings(tmp_path)
    hooks = settings.get("hooks", {})
    for entries in hooks.values():
        for entry in entries:
            for h in entry.get("hooks", []):
                assert "adr-agent" not in h.get("command", "")


def test_remove_hooks_preserves_others(tmp_path: Path):
    existing = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-tool"}]}
            ]
        }
    }
    save_settings(existing, tmp_path)
    add_adr_hooks(tmp_path)
    remove_adr_hooks(tmp_path)

    settings = load_settings(tmp_path)
    pre_hooks = settings["hooks"].get("PreToolUse", [])
    commands = [h["command"] for e in pre_hooks for h in e.get("hooks", [])]
    assert "other-tool" in commands


def test_check_hooks_present_all_present(tmp_path: Path):
    add_adr_hooks(tmp_path)
    status = check_hooks_present(tmp_path)
    assert all(status.values())


def test_check_hooks_present_none(tmp_path: Path):
    status = check_hooks_present(tmp_path)
    assert not any(status.values())


def test_settings_file_location(tmp_path: Path):
    add_adr_hooks(tmp_path)
    assert (tmp_path / ".claude" / "settings.json").exists()


def test_save_and_load_roundtrip(tmp_path: Path):
    data = {"foo": "bar", "hooks": {"SessionStart": []}}
    save_settings(data, tmp_path)
    loaded = load_settings(tmp_path)
    assert loaded["foo"] == "bar"
