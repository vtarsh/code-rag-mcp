#!/usr/bin/env python3
"""LOO test: for each real reviewer comment, test if MCP would have surfaced
the answer BEFORE the developer made the mistake.

For each comment we formulate the natural question a dev would ask and:
1. Call search() — does MCP retrieve the relevant file/doc?
2. Check if the expected answer signal is in the top results

Output: pass/fail per test + concrete gaps.
"""

import json
import urllib.request
from pathlib import Path

DAEMON = "http://localhost:8742"
PROFILE = Path(__file__).resolve().parent.parent / "profiles" / "pay-com"


def call(tool: str, args: dict, timeout: int = 120) -> str:
    req = urllib.request.Request(
        f"{DAEMON}/tool/{tool}",
        data=json.dumps(args).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()).get("result", "")
    except Exception as e:
        return f"ERROR: {e}"


# Each test: (id, description, question, signals_that_prove_hit)
# A "hit" means MCP surfaced enough info for the developer to avoid the bug.
TESTS = [
    {
        "id": "T1-paysafe-phone",
        "bug": "Added phone as required on interac payout route — breaks paysafe (no phone needed)",
        "question": "paysafe interac payout required fields consumerId email",
        "hit_signals": ["paysafe", "consumerId", "interacEtransfer", "EMAIL"],
        "miss_note": "MCP should show paysafe interac e-transfer uses only consumerId+type, no phone",
    },
    {
        "id": "T2-reusable-payouts-logic",
        "bug": "reusablePayouts flag uses wrong logic (requires phone instead of approved+email)",
        "question": "reusablePayouts flag logic email approved APM payout",
        "hit_signals": ["reusablePayouts", "email", "approved"],
        "miss_note": "MCP should show reusablePayouts depends on approved+email, not phone",
    },
    {
        "id": "T3-method-threading",
        "bug": "Hardcoded payment method in methods/payout.js instead of threading from request",
        "question": "grpc-apm provider methods payout sale refund pass payment method from request",
        "hit_signals": ["paymentMethod", "req.paymentMethod", "methods/sale"],
        "miss_note": "MCP should show other providers' methods/*.js pattern of threading paymentMethod from request",
    },
    {
        "id": "T4-webhook-tx-types",
        "bug": "Webhook handler routes sale/refund but forgets payout tx type",
        "question": "workflow-provider-webhooks payper handle-activities transaction type routing sale refund payout",
        "hit_signals": ["tx_action", "payout", "sale", "refund"],
        "miss_note": "MCP should show webhook tx_action enum needs payout branch",
    },
    {
        "id": "T5-s2s-scope",
        "bug": "Added s2s flow support in call-providers-initialize.js — s2s not in scope for payper",
        "question": "payper s2s flow initialize supported server-to-server provider",
        "hit_signals": ["s2s", "server-to-server", "initialize"],
        "miss_note": "MCP should NOT show payper as s2s-supported (or show it clearly isn't)",
    },
]


def check_hit(result: str, signals: list[str]) -> tuple[int, list[str]]:
    """Return (hit_count, matched_signals)."""
    result_lower = result.lower()
    matched = [s for s in signals if s.lower() in result_lower]
    return len(matched), matched


def main():
    print("=" * 75)
    print("LOO test on real reviewer comments (2026-04-10)")
    print("=" * 75)

    results = []
    for test in TESTS:
        print(f"\n--- {test['id']} ---")
        print(f"  Bug: {test['bug']}")
        print(f"  Query: {test['question']}")

        output = call("search", {"query": test["question"]})
        if output.startswith("ERROR:"):
            print(f"  {output}")
            results.append({**test, "hit_count": 0, "matched": [], "output_len": 0, "status": "ERROR"})
            continue

        hit_count, matched = check_hit(output, test["hit_signals"])
        total = len(test["hit_signals"])
        score = hit_count / total if total > 0 else 0
        status = "PASS" if score >= 0.75 else ("PARTIAL" if score >= 0.33 else "FAIL")

        print(f"  Result len: {len(output)}")
        print(f"  Signals matched: {hit_count}/{total} — {', '.join(matched) if matched else 'none'}")
        print(f"  Status: {status} ({score:.0%})")

        # Save raw output
        out_file = PROFILE / f"loo_real_{test['id']}.txt"
        out_file.write_text(output)

        results.append({
            **test,
            "hit_count": hit_count,
            "matched": matched,
            "total_signals": total,
            "score": score,
            "status": status,
            "output_file": str(out_file),
        })

    # Summary
    print(f"\n{'=' * 75}")
    print("SUMMARY")
    print("=" * 75)
    passed = sum(1 for r in results if r.get("status") == "PASS")
    partial = sum(1 for r in results if r.get("status") == "PARTIAL")
    failed = sum(1 for r in results if r.get("status") == "FAIL")
    errored = sum(1 for r in results if r.get("status") == "ERROR")
    print(f"PASS: {passed}/{len(TESTS)}  PARTIAL: {partial}  FAIL: {failed}  ERROR: {errored}")

    print(f"\n{'ID':<24} {'Score':<10} {'Status':<10} {'Matched':<30}")
    print("-" * 75)
    for r in results:
        matched_str = ",".join(r.get("matched", [])) or "-"
        print(f"{r['id']:<24} {r.get('score', 0):<10.0%} {r.get('status', '-'):<10} {matched_str[:30]:<30}")

    # Save JSON
    out = PROFILE / "loo_real_comments_results.json"
    with open(out, "w") as f:
        json.dump({"results": results}, f, indent=2)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
