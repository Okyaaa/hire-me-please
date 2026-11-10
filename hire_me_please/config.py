from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(os.environ.get("HIRE_ME_PLEASE_HOME", Path.cwd())).resolve()
PROFILE_PATH = ROOT / "profile.yaml"
EXAMPLE_PROFILE_PATH = Path(__file__).resolve().parent.parent / "profile.example.yaml"
DB_PATH = ROOT / "jobs.db"
OUTPUT_DIR = ROOT / "output"
BROWSER_DIR = ROOT / ".browser-profile"


@dataclass
class Config:
    profile: dict
    resume: str

    @property
    def model(self) -> str:
        return self.profile.get("model", "claude-opus-5-5")

    @property
    def search(self) -> dict:
        return self.profile.get("search", {})

    @property
    def apply(self) -> dict:
        return self.profile.get("apply", {})

    @property
    def personal(self) -> dict:
        return self.profile.get("personal", {})

    @property
    def full_name(self) -> str:
        p = self.personal
        return f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()


def load() -> Config:
    load_dotenv(ROOT / ".env")
    if not PROFILE_PATH.exists():
        raise SystemExit("profile.yaml not found. Run `hire-me-please init` first.")
    profile = yaml.safe_load(PROFILE_PATH.read_text()) or {}
    resume_path = ROOT / profile.get("resume_path", "resume.md")
    if not resume_path.exists():
        raise SystemExit(f"Base resume not found at {resume_path}. Write it in Markdown.")
    return Config(profile=profile, resume=resume_path.read_text())
