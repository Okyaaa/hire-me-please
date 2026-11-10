"""All Claude calls. Each returns a validated Pydantic object via structured outputs.

Two backends:
- "claude-cli": runs the Claude Code CLI (`claude -p`), signed in with your Claude plan. No API key.
- "api": the Anthropic API with ANTHROPIC_API_KEY, billed per token.
"auto" (the default) uses the API when a key is set, otherwise the CLI.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import TypeVar

import anthropic
import yaml
from pydantic import BaseModel, Field

from .config import Config

T = TypeVar("T", bound=BaseModel)

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4)
    return _client


# ---------- output schemas ----------

class JobScore(BaseModel):
    score: int = Field(description="0-100 fit score")
    verdict: str = Field(description="apply | maybe | skip")
    reasons: list[str] = Field(description="Top reasons for the score, most important first")
    red_flags: list[str] = Field(description="Dealbreakers or gaps, e.g. sponsorship, seniority, location")


class TailoredDocs(BaseModel):
    resume_markdown: str
    cover_letter_markdown: str
    changes: list[str] = Field(description="What was changed vs. the base resume and why")


class FieldAnswer(BaseModel):
    field_id: str
    value: str = Field(description="Text to enter, or the exact option label(s) to pick, joined by ' | ' for multi-select. Empty string to leave blank.")
    needs_human: bool = Field(description="True when the answer is not supported by the candidate's facts")
    note: str = Field(description="Short reason, shown to the user when needs_human is true")


class FormPlan(BaseModel):
    answers: list[FieldAnswer]


# ---------- shared context ----------

def candidate_context(cfg: Config) -> str:
    """Stable per-candidate system prompt. Cached, so every job after the first reuses it."""
    profile = {k: v for k, v in cfg.profile.items() if k not in ("sources", "model")}
    return (
        "You are helping a job seeker apply to roles. You act strictly on their behalf and in their interest.\n"
        "Ground rules:\n"
        "- Use ONLY facts stated in the candidate profile and resume below. Never invent employers, titles, "
        "dates, degrees, skills, metrics, certifications, clearances or work authorization.\n"
        "- You may rephrase, reorder, emphasise and mirror the job's terminology where the underlying fact is true.\n"
        "- Write in plain, specific, confident English. No clichés, no filler.\n"
        "- In notes and reasons, refer to the candidate as \"the candidate\"; never assume their gender.\n\n"
        f"<candidate_profile>\n{yaml.safe_dump(profile, sort_keys=False)}</candidate_profile>\n\n"
        f"<base_resume>\n{cfg.resume}\n</base_resume>"
    )


def job_block(job) -> str:
    return (
        f"<job>\nCompany: {job['company']}\nTitle: {job['title']}\nLocation: {job['location']}\n"
        f"URL: {job['url']}\n\n{job['description']}\n</job>"
    )


def backend(cfg: Config) -> str:
    choice = cfg.profile.get("backend", "auto")
    if choice == "auto":
        return "api" if os.environ.get("ANTHROPIC_API_KEY") else "claude-cli"
    return choice


def _parse(cfg: Config, prompt: str, schema: type[T], effort: str, max_tokens: int = 16000) -> T:
    if backend(cfg) == "claude-cli":
        return _parse_cli(cfg, prompt, schema, effort)
    return _parse_api(cfg, prompt, schema, effort, max_tokens)


def _parse_cli(cfg: Config, prompt: str, schema: type[T], effort: str) -> T:
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("Claude Code CLI not found. Install it: npm install -g @anthropic-ai/claude-code")
    cmd = [
        exe, "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(schema.model_json_schema()),
        "--system-prompt", candidate_context(cfg),
        "--model", cfg.model,
        "--effort", effort,
        # A pure text task: no file, shell or web tools, no MCP servers, no saved session.
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    # Drop API credentials so the CLI uses the signed-in Claude plan, not API billing.
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    with tempfile.TemporaryDirectory() as empty_dir:  # no project CLAUDE.md or settings get picked up
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env, cwd=empty_dir, timeout=900)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        detail = (proc.stderr or proc.stdout).strip()[-500:]
        if "login" in detail.lower() or "auth" in detail.lower():
            detail += "\nSign in once with: claude auth login"
        raise RuntimeError(f"Claude CLI failed (exit {proc.returncode}): {detail}") from None
    if data.get("is_error"):
        msg = str(data.get("result", ""))[:500]
        if "login" in msg.lower() or "auth" in msg.lower():
            msg += "\nSign in once with: claude auth login"
        raise RuntimeError(f"Claude CLI error: {msg}")
    out = data.get("structured_output")
    if out is None:  # fall back to JSON in the text result
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(data.get("result", "")).strip())
        out = json.loads(text)
    return schema.model_validate(out)


def _parse_api(cfg: Config, prompt: str, schema: type[T], effort: str, max_tokens: int) -> T:
    response = client().messages.parse(
        model=cfg.model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": candidate_context(cfg), "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": effort},
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError(f"Incomplete response from Claude (stop_reason={response.stop_reason})")
    return response.parsed_output


# ---------- tasks ----------

def score_job(cfg: Config, job) -> JobScore:
    prompt = (
        f"{job_block(job)}\n\n"
        "Score how good a fit this job is for the candidate, 0-100.\n"
        "90+: strong match on skills, seniority and location; apply immediately.\n"
        "70-89: good match with minor gaps. 40-69: stretch. <40: poor fit.\n"
        "Hard requirements the candidate clearly fails (sponsorship, location, required clearance, "
        "years of experience far off, wrong discipline) cap the score at 30 and go in red_flags."
    )
    return _parse(cfg, prompt, JobScore, effort="low", max_tokens=4000)


def tailor(cfg: Config, job) -> TailoredDocs:
    prompt = (
        f"{job_block(job)}\n\n"
        "1. Rewrite the base resume for this job, in the same Markdown structure. Lead with the most relevant "
        "experience, rewrite bullets to foreground what this job asks for, mirror its keywords where true, and "
        "cut what is irrelevant. Keep it to what fits on one page (two for 10+ years). Every fact must already "
        "be in the base resume or profile.\n"
        "2. Write a cover letter in Markdown, 200-300 words, addressed to the hiring team at the company. "
        "Open with why this specific role, give two concrete proof points from the resume, close briefly. "
        "Sign with the candidate's name. No placeholders like [Company].\n"
        "3. List the changes you made."
    )
    return _parse(cfg, prompt, TailoredDocs, effort="medium")


def plan_form(cfg: Config, job, fields: list[dict], cover_letter: str | None) -> FormPlan:
    consent = cfg.apply.get("accept_privacy_consents", False)
    prompt = (
        f"{job_block(job)}\n\n"
        + (f"<cover_letter>\n{cover_letter}\n</cover_letter>\n\n" if cover_letter else "")
        + "<form_fields>\n" + json.dumps(fields, indent=1) + "\n</form_fields>\n\n"
        "Decide what to enter in each form field of this job application. Return one answer per field_id.\n"
        "- Text fields: the exact text. Use profile values verbatim for name, email, phone, links, location.\n"
        "- select / radio / checkbox / combobox: value must be one of the listed options, copied exactly "
        "(for comboboxes without options, give the text to type).\n"
        "- Open-ended questions (why this company, describe a project): write a concise, specific answer "
        "of 50-150 words from the resume and job. If a cover letter field is a textarea, paste the cover letter.\n"
        "- Voluntary EEO / demographic questions: use the profile's eeo answers, else the 'decline' option.\n"
        f"- Privacy-policy / data-processing consent checkboxes: {'tick them' if consent else 'set needs_human'}.\n"
        "- Any factual question the profile and resume do not answer (e.g. previously employed here, "
        "referral name, security clearance, specific licenses): set needs_human=true and value to your best "
        "safe guess or empty. Never guess work authorization, sponsorship, criminal history or legal attestations.\n"
        "- Optional fields you have nothing truthful for: empty value, needs_human=false."
    )
    return _parse(cfg, prompt, FormPlan, effort="medium")
