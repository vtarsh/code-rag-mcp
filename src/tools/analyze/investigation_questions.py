"""Hand-curated investigation questions for analyze_task.

Each question is validated via blind LOO eval (see logs/blind_tests/).
Questions must prove BAS improvement before being added.

Toggle via env var: CODE_RAG_QUESTIONS_MODE = "off" | "q1" | "q1+q2" | ...
Default: "off" (no questions injected).
"""
from __future__ import annotations

import os


# Registry of validated questions. Keys are question IDs, values are the text.
# Add a new question ONLY after blind LOO test shows +BAS and vector uniqueness
# vs existing questions.
_QUESTIONS: dict[str, str] = {
    "q1": (
        "Compare each method and webhook handler with 2 sibling providers: "
        "are all tx types (sale/refund/payout) routed? "
        "Is paymentMethod threaded from request, not hardcoded?"
    ),
}


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
    """analyze_task orchestrator adapter — returns markdown block or empty string."""
    mode = os.environ.get("CODE_RAG_QUESTIONS_MODE", "off")
    if mode == "off":
        return ""
    selected: list[str] = []
    for qid, qtext in _QUESTIONS.items():
        if qid in mode:
            selected.append(qtext)
    return render_investigation_section(selected)
