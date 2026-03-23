# Deep Analysis Tiers

## Task Classification → Agent Assignment

Before launching deep analysis agents, classify each task by complexity:

### Tier 1: Full Provider/APM Integration (1 agent per 1 task)
**Criteria**: New provider from scratch, 5+ repos expected, summary contains "integration", "provider integration", "APM integration"
**Agent time**: 15-30 min per task
**What agent does**:
- Read ALL files_changed, understand the flow end-to-end
- Trace the payment flow: API → gateway → provider → webhooks → completion
- Check which standard PI repos are present vs missing
- Read actual code in raw/ to understand WHY each repo was touched
- Verify graph edges match real dependencies
- Compare with similar completed integrations
- Output: full flow diagram + gap analysis + improvement suggestions

**Examples**: PI-40 (trustly integration), PI-5 (okto cash APM), PI-1 (rapyd integrations)

### Tier 2: Provider Extension (1 agent per 2-3 tasks)
**Criteria**: Add method/webhook/feature to existing provider, 2-5 repos, summary contains "add", "implement", "verification", "payout"
**Agent time**: 5-10 min per task
**What agent does**:
- Check which method/feature is being added
- Verify standard companion repos (credentials, features, webhooks)
- Check if new proto fields or payment method types needed
- Compare with same provider's other tasks

**Examples**: PI-54 (trustly verification), PI-53 (trustly payouts), PI-55 (nuvei pay by bank)

### Tier 3: Cross-cutting / Config Changes (1 agent per 3-4 tasks)
**Criteria**: Migration, audit, config update, bulk change, 1-3 repos
**Agent time**: 3-5 min per task
**What agent does**:
- Identify the change type (migration, config, fix)
- Check if cascade/bulk detection should fire
- Verify keyword matching works

**Examples**: PI-36 (seed config), PI-49 (PCI update), CORE-2435 (typescript migrate)

### Tier 4: Simple Fixes (1 agent per 5-10 tasks)
**Criteria**: Bug fix, refactor, 1 repo, short description
**Agent time**: 1-2 min per task
**What agent does**:
- Quick: does tool find the repo? If yes → pass. If no → classify root cause.
- No deep flow analysis needed.

**Examples**: CORE-2591 (cache fix), CORE-2617 (error format)

## How to Classify Automatically
```python
def classify_task(repos_count, summary):
    summary_lower = summary.lower()
    if repos_count >= 5 and any(kw in summary_lower for kw in ['integration', 'provider', 'apm']):
        return 'tier1'
    elif repos_count >= 2 and any(kw in summary_lower for kw in ['add', 'implement', 'verification', 'payout', 'webhook']):
        return 'tier2'
    elif repos_count >= 2:
        return 'tier3'
    else:
        return 'tier4'
```

## What We Did vs What's Needed

**This session**: All 361 tasks got Tier 3-4 level analysis (automated classification + grep + benchmark comparison). Good for finding patterns, insufficient for understanding flows.

**Next session**: Run Tier 1 analysis on the ~15 full PI integrations. This will reveal:
- Which parts of the standard PI flow the tool covers well
- Where the flow breaks (which hop is missing)
- Whether the code structure matches what the graph captures
