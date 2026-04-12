"""Dynamic investigation-questions section for analyze_task.

Instead of canned keyword-triggered warnings, this module asks Gemini to
generate task-specific investigation questions: concrete, actionable
checks a senior engineer would run BEFORE writing any code. Each question
should point at a file, function, or provider comparison that can be
verified via MCP tools (search, provider_type_map, trace_chain, etc.).

This is the "true proactivity" path — the questions are contextual, not
regex-triggered, and they scale with the task description richness
rather than with our keyword coverage.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass

_MODEL = os.environ.get("CODE_RAG_QUESTIONS_MODEL", "gemini-2.5-flash")
_MAX_QUESTIONS = int(os.environ.get("CODE_RAG_QUESTIONS_MAX", "8"))


# The prompt template is the primary knob for autoresearch tuning.
# Variants should be injected via CODE_RAG_QUESTIONS_PROMPT env var, or
# by calling generate_investigation_questions(..., prompt_template=...)
# directly from a tuning loop.
DEFAULT_PROMPT_TEMPLATE = """\
You are a senior payments-infra engineer doing a pre-implementation
safety review. Given the task description below and a short summary of
files that similar tasks historically touch, write {n} critical
investigation questions the implementer MUST answer BEFORE writing any
code.

Requirements for each question:
- Name a specific file, function, provider, or route — no generic advice.
- Be answerable via concrete actions: grep, file read, provider comparison, git log, or a specific MCP tool call (provider_type_map, trace_chain, search, trace_field).
- Focus on: cross-provider impact, scope boundaries, fallback chains,
  convention violations vs sibling providers, and enumeration
  completeness (switch / enum cases).
- One question per line, numbered, no preamble, no trailing notes.

TASK DESCRIPTION:
{description}

RELEVANT FILES (historical shared-file patterns matched or likely touched):
{shared_summary}

EXISTING PROVIDER CONTEXT: {provider}

Output the {n} questions now:
"""


def _call_gemini(prompt: str) -> str | None:
    """Minimal Gemini text call. Rotates keys on quota."""
    try:
        from src.config import GEMINI_API_KEYS
    except Exception:
        return None
    if not GEMINI_API_KEYS:
        return None
    try:
        from google import genai
    except ImportError:
        return None
    last_err = None
    for idx, api_key in enumerate(GEMINI_API_KEYS):
        try:
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model=_MODEL,
                contents=[{"role": "user", "parts": [{"text": prompt}]}],
                config={"temperature": 0.0},
            )
            return (resp.text or "").strip()
        except Exception as e:
            last_err = e
            s = str(e)
            if "429" in s or "RESOURCE_EXHAUSTED" in s or "quota" in s.lower():
                print(f"[investigation_questions] key #{idx+1} quota, rotating", file=sys.stderr)
                continue
            print(f"[investigation_questions] Gemini call failed: {e}", file=sys.stderr)
            return None
    print(f"[investigation_questions] all keys exhausted: {last_err}", file=sys.stderr)
    return None


def _summarise_shared_files(shared_file_patterns: list[str]) -> str:
    """One-line-per-file summary used to seed Gemini context."""
    if not shared_file_patterns:
        return "(no shared files detected yet)"
    return "\n".join(f"- {p}" for p in shared_file_patterns[:10])


def generate_investigation_questions(
    description: str,
    shared_file_patterns: list[str] | None = None,
    provider: str = "",
    n: int = _MAX_QUESTIONS,
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE,
) -> list[str]:
    """Return a list of Gemini-generated investigation questions.

    Returns an empty list if Gemini is unavailable or the response
    parses to zero questions — the caller should fall back to the
    keyword-triggered shared_files branch in that case.
    """
    if not description or not description.strip():
        return []
    prompt = prompt_template.format(
        description=description.strip(),
        shared_summary=_summarise_shared_files(shared_file_patterns or []),
        provider=provider or "(unknown)",
        n=n,
    )
    raw = _call_gemini(prompt)
    if not raw:
        return []
    # Parse numbered list.
    lines = []
    for line in raw.splitlines():
        m = re.match(r"^\s*(?:\d+[.)]|\-|\*)\s*(.+?)\s*$", line)
        if m:
            q = m.group(1).strip()
            if q and len(q) >= 10:
                lines.append(q)
    return lines[:n]


def render_investigation_section(questions: list[str]) -> str:
    """Render the questions list as a markdown section."""
    if not questions:
        return ""
    out = "## 🤔 Investigation Questions — answer these before writing code\n\n"
    out += "_Dynamically generated for this task. Each question is actionable via search/provider_type_map/trace_chain._\n\n"
    for i, q in enumerate(questions, 1):
        out += f"{i}. {q}\n"
    out += "\n"
    return out


def section_investigation_questions(ctx) -> str:
    """analyze_task orchestrator adapter — takes AnalysisContext, returns markdown.

    Short-circuits to empty when:
      - description too short
      - CODE_RAG_DISABLE_INVESTIGATION_QUESTIONS env var is set
      - Gemini returns nothing
    """
    if os.environ.get("CODE_RAG_DISABLE_INVESTIGATION_QUESTIONS") == "1":
        return ""
    description = getattr(ctx, "description", "") or ""
    if len(description.strip()) < 20:
        return ""
    provider = getattr(ctx, "provider", "") or ""

    # Seed the LLM with shared_file patterns. When the APM context gate
    # opens, pass the FULL shared_files list (not just keyword-matched
    # ones) so Gemini has visibility into every high-risk file even when
    # the task description uses paraphrased wording that misses keyword
    # triggers. This is the paraphrase-robustness fix.
    try:
        from src.config import SHARED_FILES
        from src.tools.analyze.shared_sections import _has_apm_context
        patterns: list[str] = []
        if _has_apm_context(description, provider):
            for entry in SHARED_FILES:
                pat = entry.get("path_pattern", "")
                if pat:
                    patterns.append(pat)
    except Exception:
        patterns = []

    template = os.environ.get("CODE_RAG_QUESTIONS_PROMPT") or DEFAULT_PROMPT_TEMPLATE

    questions = generate_investigation_questions(
        description=description,
        shared_file_patterns=patterns,
        provider=provider,
        prompt_template=template,
    )
    return render_investigation_section(questions)
