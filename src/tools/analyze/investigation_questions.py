"""Hand-curated investigation questions for analyze_task.

Each question is validated via blind LOO eval (see logs/blind_tests/).
Questions must prove BAS improvement before being added.

Questions can declare task-type triggers (keywords that must appear in the
task description) so a question only activates when it's actually relevant.
This avoids off-target noise on tasks where the question would not help.

Toggle via env var: CODE_RAG_QUESTIONS_MODE = "off" | "q1" | "q1+q2" | ...
Default: "off" (no questions injected).
"""

from __future__ import annotations

import os
from typing import TypedDict


class _Question(TypedDict):
    text: str
    triggers: list[str]


# Registry of validated questions. Keys are question IDs.
# Add a new question ONLY after blind LOO test shows +BAS and vector uniqueness
# vs existing questions.
#
# Triggers: list of case-insensitive substrings that must appear in the task
# description for the question to activate. Empty list = always active.
# Validation results for trigger scoping: see logs/blind_tests/generalization_summary.md
_QUESTIONS: dict[str, _Question] = {
    "q1": {
        "text": (
            "Compare each method and webhook handler with 2 sibling providers: "
            "are all tx types (sale/refund/payout) routed? "
            "Is paymentMethod threaded from request, not hardcoded?"
        ),
        # Validated +1.5 BAS on PI-60 (full APM with payout).
        # Off-target on PI-14 (−1.0) and neutral on PI-5.
        # Activate only when task explicitly mentions payout — that's where
        # architectural cross-provider routing checks yield measurable lift.
        "triggers": ["payout"],
    },
}


def _task_matches_triggers(description: str, triggers: list[str]) -> bool:
    """Check if task description matches any trigger keyword (substring, case-insensitive).

    Empty triggers list = always active (no gating).
    Empty description with non-empty triggers = never active (fail-closed).
    """
    if not triggers:
        return True
    if not description:
        return False
    desc_lower = description.lower()
    return any(trigger.lower() in desc_lower for trigger in triggers)


def render_investigation_section(questions: list[str]) -> str:
    """Render the questions list as a markdown section."""
    if not questions:
        return ""
    out = "## 🤔 Investigation Questions — answer these before writing code\n\n"
    out += "_Each question is actionable via search / provider_type_map / trace_chain._\n\n"
    for i, q in enumerate(questions, 1):
        out += f"{i}. {q}\n"
    out += "\n"
    return out


def section_investigation_questions(ctx) -> str:
    """analyze_task orchestrator adapter — returns markdown block or empty string.

    A question is included only if all three hold:
      1. CODE_RAG_QUESTIONS_MODE != "off"
      2. The question ID appears in CODE_RAG_QUESTIONS_MODE
      3. The question's triggers match the task description (empty triggers = always)
    """
    mode = os.environ.get("CODE_RAG_QUESTIONS_MODE", "off")
    if mode == "off":
        return ""
    description = (getattr(ctx, "description", "") or "").strip()
    selected: list[str] = []
    for qid, qdef in _QUESTIONS.items():
        if qid not in mode:
            continue
        if not _task_matches_triggers(description, qdef["triggers"]):
            continue
        selected.append(qdef["text"])
    return render_investigation_section(selected)
