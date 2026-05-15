"""H3 repro: confirms doc-top-1 over-representation correlates with penalty-skip path.

Expected output: pen-applied doc-top-1 fraction ≈ 3.7%, pen-skipped fraction ≈ 25.7% (~7-9x delta).
"""

import json
import re

d = json.load(open("bench_runs/jira_e2e_wide_off_session2.json"))

DOC = re.compile(
    r"\b(test|tests|spec|specs|docs?|documentation|readme|guide|guides|tutorial|checklist|framework|matrix|severity|sandbox|overview|reference|rules|gotcha|gotchas|how\s+to)\b",
    re.I,
)
CODE = re.compile(r"(?:\b[a-z][a-zA-Z0-9]*\([^)]*\)|\b[A-Z][A-Z0-9_]{2,}\b|[a-z]+_[a-z_]+|\.(?:js|ts|py|go|proto)\b)")
REPO = re.compile(r"\b(?:grpc-|express-|next-web-|workflow-|k8s-)[a-z0-9-]+\b", re.I)
RO = re.compile(
    r"^\s*(?:grpc-|express-|next-web-|workflow-|k8s-|backoffice-)[a-z0-9-]+(?:\s+(?:repo|repository))?\s*$", re.I
)
PO = re.compile(
    r"^\s*(?:nuvei|trustly|payper|volt|ppro|paynearme|aeropay|fonix|paysafe|worldpay|skrill|aircash|okto|interac|neosurf|rapyd|epay|fortumo)\s*$",
    re.I,
)
CD = re.compile(
    r"\b(apm|tokenizer|vault|sepa|voucher|integrate|integration|integrations|provider\s+integration|how\s+does|how\s+is|pattern|repo|repository)\b",
    re.I,
)
SC = re.compile(
    r"(?:\.(?:js|ts|tsx|jsx|py|go|proto)\b|\b[a-z][a-zA-Z]{8,}[A-Z][a-zA-Z]+\b|(?:[a-z]+_[a-z_]+\s+){1,}[a-z]+_[a-z_]+|\b(?:doNotExpire|signalWithStart|activateWorkflow)\b)"
)


def query_wants_docs(q):
    if DOC.search(q or ""):
        return True
    if RO.search(q or ""):
        return True
    if PO.search(q or ""):
        return True
    if CD.search(q or "") and not SC.search(q or ""):
        return True
    if CODE.search(q or "") or REPO.search(q or ""):
        return False
    return 2 <= len((q or "").split()) <= 15


DOC_TYPES = {"docs", "task", "gotchas", "reference", "dictionary", "provider_doc"}
counts = {"pen_applied_doc_top1": 0, "pen_applied_total": 0, "pen_skipped_doc_top1": 0, "pen_skipped_total": 0}
for r in d["eval_per_query"]:
    tops = r.get("top_files", [])
    if not tops:
        continue
    t1_ft = tops[0].get("file_type", "")
    is_doc = query_wants_docs(r["query"])
    if not is_doc:
        counts["pen_applied_total"] += 1
        if t1_ft in DOC_TYPES:
            counts["pen_applied_doc_top1"] += 1
    else:
        counts["pen_skipped_total"] += 1
        if t1_ft in DOC_TYPES:
            counts["pen_skipped_doc_top1"] += 1
print(counts)
print(f"Penalty applied doc-top-1: {counts['pen_applied_doc_top1'] / counts['pen_applied_total']:.4f}")
print(f"Penalty skipped doc-top-1: {counts['pen_skipped_doc_top1'] / counts['pen_skipped_total']:.4f}")
