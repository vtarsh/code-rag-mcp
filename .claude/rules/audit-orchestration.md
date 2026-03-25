# Audit Orchestration (Provider Implementations)

## Why This Exists

PI-60 post-mortem: 15 agents across 3 cycles missed async refund flow, gave 3 harmful recommendations. Root cause: agents audit code in isolation without understanding runtime execution order. Solution: step-scoped auditing with flow context injection.

## Three-Phase Audit

### Phase 1: Step-Scoped Audit (parallel)

For each step in the execution flow, spawn a separate agent with injected flow context.

**Before launching agents:**
1. Run `python scripts/build_audit_context.py --task {TASK_ID} --provider {PROVIDER}` to generate flow context
2. Collect PR review comments if any: `gh api repos/pay-com/{repo}/pulls/{PR}/comments`
3. Determine task scope from files_changed (which methods: initialize, sale, refund, void, getStatus)

**Per-agent prompt structure:**
Each agent receives:
- Its step in the flow (repo, role, methods being implemented)
- Previous step (what feeds into it, what data it receives)
- Next step (what consumes its output, what breaks if wrong)
- Reference provider code (how volt/paysafe/trustly do this step)
- Task scope (which methods are MVP, which are out-of-scope)
- Reviewer constraints (PR comments as immutable decisions)

**Typical step breakdown for APM provider:**
1. Agent: grpc-apm-{provider} (initialize + sale methods)
2. Agent: grpc-apm-{provider} (refund method + async flow)
3. Agent: workflow-provider-webhooks (parse-payload + handle-activities)
4. Agent: Cross-repo wiring (credentials, features, gateway routing, webhook routing)
5. Agent: Tests (run all test suites, verify coverage)

### Phase 2: Deep Analysis Verification (parallel)

Every HIGH or CRITICAL finding from Phase 1 MUST be verified by a separate Deep Analysis agent before becoming a recommendation.

**Verification protocol:**
1. grep raw/{repo}/ for the method/field mentioned — does it exist?
2. Check if reference providers actually do this (grep raw/grpc-apm-volt/, raw/grpc-apm-paysafe/)
3. Check if platform handles this generically (grep raw/grpc-payment-gateway/)
4. Check if finding contradicts reviewer comments
5. If any check fails → finding is INVALID, remove entirely

**Deep Analysis agents are single-question agents:** "Is {finding} a real bug? Verify by checking {repos}."

### Phase 3: Consolidation

Main session collects verified findings and:
1. Groups by flow step (not by repo)
2. Adds cross-step impact notes ("bug in step 3 means step 5 gets wrong status")
3. Deduplicates (multiple agents may find same issue from different angles)
4. Final severity calibration using impact-audit.md rules

## Key Principle

**Narrow + context >> broad without context.** Deep Analysis at 100% accuracy beats broad audit at 70-81%. When in doubt, make agents narrower with more context, not broader with less.

## What Agents Must NOT Do

- Recommend using a method without grepping it exists first
- Override reviewer-confirmed decisions
- Flag out-of-scope features as CRITICAL (scope = what's in files_changed)
- Assume API formats without checking provider docs
- Flag generic platform handling as provider-specific gap

## Anti-Pattern: "Audit Repo X"

NEVER prompt an agent with just "audit repo X for bugs". This is the root cause of PI-60 failures. Always scope to a specific flow step with adjacent-step context.

## Anti-Pattern: "Stop at Sufficient Explanation"

When tracing a flow through services, agents MUST go all the way from HTTP request to HTTP response through EVERY layer. Never stop at the first "sufficient" explanation.

Example: tracing `syncFlow: true` — don't stop at workflow.js (startChild vs executeChild). Go further to express-webhooks route handler to see WHERE the HTTP 200 is actually returned. The answer might be different from what the intermediate layer suggests.

Rule: if an agent is asked to trace a flow, it must read the actual code at EVERY hop, not assume based on one layer.
