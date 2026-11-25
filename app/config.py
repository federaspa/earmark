"""Paths and settings shared by every component."""

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "inbox"
PROCESSED = ROOT / "processed"
FAILED = ROOT / "failed"
LOGS = ROOT / "logs"
RECORDINGS = ROOT / "recordings"
PROMPTS = {"meeting": ROOT / "prompts" / "filer.md", "voice-note": ROOT / "prompts" / "voice-note.md"}
LOCAL_PROMPT = ROOT / "prompts" / "local.md"  # gitignored: examples from the real vault


def _path(value: str) -> Path:
    return ROOT / Path(value).expanduser()  # absolute paths stay as they are


def load() -> dict:
    """config.toml (local, not in git), or config.example.toml if there is none yet."""
    path = ROOT / "config.toml"
    with open(path if path.exists() else ROOT / "config.example.toml", "rb") as f:
        cfg = tomllib.load(f)
    cfg["vault"]["path"] = _path(cfg["vault"]["path"])
    cfg["recorder"]["audiorec"] = _path(cfg["recorder"]["audiorec"])
    return cfg


def load_env() -> None:
    """Read KEY=VALUE lines from .env into os.environ (without overriding)."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))
