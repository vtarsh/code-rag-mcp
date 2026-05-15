# F1 done — MPS-aware memguard

Applied F1 from `.claude/debug/verdict.md`: `memory_pressure()` now classifies on
MPS driver pool as a third signal alongside RSS and sys avail. On Apple Silicon
the `torch.mps.driver_allocated_memory()` reading is the only metric that
actually tracks the pool growth that causes OOM on the docs-tower build; RSS
underreports it by ~3×.

## Files modified

| File | md5 |
|---|---|
| `src/index/builders/_memguard.py` | `8fb4b4614c69b6b958ef35ba9bdde9e4` |
| `tests/test_memguard.py` | `9549518c43e848538edd746e0bfc1d60` |

## Unified diff — `src/index/builders/_memguard.py` (F1 hunks only)

```diff
@@ class Limits:
     rss_soft_bytes: int
     rss_hard_bytes: int
     sys_avail_soft_bytes: int
     sys_avail_hard_bytes: int
+    mps_soft_bytes: int
+    mps_hard_bytes: int
     daemon_port: int

@@ def get_limits() -> Limits:
         sys_avail_soft_bytes=int(float(os.getenv("CODE_RAG_EMBED_SYS_AVAIL_SOFT_GB", "2")) * _GIB),
         sys_avail_hard_bytes=int(float(os.getenv("CODE_RAG_EMBED_SYS_AVAIL_HARD_GB", "0.8")) * _GIB),
+        mps_soft_bytes=int(float(os.getenv("CODE_RAG_EMBED_MPS_SOFT_GB", "5.0")) * _GIB),
+        mps_hard_bytes=int(float(os.getenv("CODE_RAG_EMBED_MPS_HARD_GB", "6.0")) * _GIB),
         daemon_port=int(os.getenv("CODE_RAG_DAEMON_PORT", "8742")),
     )


+def _mps_driver_bytes() -> int:
+    """Return MPS driver pool size in bytes, or 0 when unavailable.
+
+    On Apple Silicon the MPS allocator retains pages that `gc.collect()` +
+    `empty_cache()` cannot return to the OS, so psutil RSS underreports real
+    pressure by ~3x (see debug/verdict.md H11). Reading
+    `torch.mps.driver_allocated_memory()` gives us the true effective usage.
+    """
+    with contextlib.suppress(Exception):
+        import torch
+
+        if torch.backends.mps.is_available():
+            return int(torch.mps.driver_allocated_memory())
+    return 0
+
+
 def pause_daemon(port: int | None = None, timeout: float = 5.0) -> bool:
@@ def memory_pressure(limits: Limits | None = None) -> tuple[str, int, int]:
-    """Classify current pressure. Returns (level, rss_bytes, avail_bytes).
+    """Classify current pressure. Returns (level, effective_rss, avail_bytes).
+
+    On Apple Silicon the returned ``effective_rss`` is ``max(rss, mps_bytes)``
+    so downstream log lines surface the higher signal (the MPS driver pool
+    often exceeds process RSS by 3x and is what actually triggers OOM).
@@
-    - ``"soft"`` — RSS at soft OR sys avail at soft. Caller should compact +
-      ``free_memory`` + re-check, and maybe sleep.
-    - ``"hard"`` — RSS at hard OR sys avail at hard. Caller should ``sys.exit(0)``
-      after writing any pending checkpoint.
+    - ``"soft"`` — RSS at soft OR sys avail at soft OR MPS pool at soft.
+      Caller should compact + ``free_memory`` + re-check, and maybe sleep.
+    - ``"hard"`` — RSS at hard OR sys avail at hard OR MPS pool at hard.
+      Caller should ``sys.exit(0)`` after writing any pending checkpoint.
@@
     rss = psutil.Process().memory_info().rss
     avail = psutil.virtual_memory().available
-    if rss >= limits.rss_hard_bytes or avail <= limits.sys_avail_hard_bytes:
-        return "hard", rss, avail
-    if rss >= limits.rss_soft_bytes or avail <= limits.sys_avail_soft_bytes:
-        return "soft", rss, avail
-    return "ok", rss, avail
+    mps_bytes = _mps_driver_bytes()
+    effective_rss = max(rss, mps_bytes)
+    if (
+        rss >= limits.rss_hard_bytes
+        or avail <= limits.sys_avail_hard_bytes
+        or mps_bytes >= limits.mps_hard_bytes
+    ):
+        return "hard", effective_rss, avail
+    if (
+        rss >= limits.rss_soft_bytes
+        or avail <= limits.sys_avail_soft_bytes
+        or mps_bytes >= limits.mps_soft_bytes
+    ):
+        return "soft", effective_rss, avail
+    return "ok", effective_rss, avail
@@ def check_and_maybe_exit(...)
+    mps_bytes = _mps_driver_bytes()
     reason = f"rss={rss / _GIB:.1f}G avail={avail / _GIB:.1f}G"
+    if mps_bytes > 0:
+        reason += f" mps={mps_bytes / _GIB:.1f}G"
     if level == "soft":
         if compact_cb is not None:
@@
         free_memory()
         level2, rss2, avail2 = memory_pressure(limits)
+        mps_bytes2 = _mps_driver_bytes()
         reason2 = f"rss={rss2 / _GIB:.1f}G avail={avail2 / _GIB:.1f}G"
+        if mps_bytes2 > 0:
+            reason2 += f" mps={mps_bytes2 / _GIB:.1f}G"
```

## Unified diff — `tests/test_memguard.py` (F1 hunks only)

```diff
@@ class TestMemoryPressure
     def test_ok_when_well_below_thresholds(self):
-        with self._patch_psutil(rss_bytes=2 * 1024**3, avail_bytes=8 * 1024**3):
+        with self._patch_psutil(rss_bytes=2 * 1024**3, avail_bytes=8 * 1024**3), \
+                patch.object(_memguard, "_mps_driver_bytes", return_value=0):
             level, _, _ = _memguard.memory_pressure()
         assert level == "ok"
     # ... identical _mps_driver_bytes=0 mock added to remaining 4 existing tests
+
+    def test_mps_pool_triggers_hard_even_when_rss_low(self):
+        # RSS and avail are both comfortable, but the MPS driver pool has
+        # crossed the hard threshold (6.5 GiB > 6.0 GiB default). On Apple
+        # Silicon this is the scenario from debug/verdict.md H11 where RSS
+        # reports 31% of the true pool.
+        with self._patch_psutil(rss_bytes=100 * 1024**2, avail_bytes=8 * 1024**3), \
+                patch.object(_memguard, "_mps_driver_bytes", return_value=int(6.5 * 1024**3)):
+            level, rss_eff, _ = _memguard.memory_pressure()
+        assert level == "hard"
+        assert rss_eff >= int(6.5 * 1024**3)
+
+    def test_mps_pool_soft_intermediate(self):
+        # MPS pool is between soft (5.0 GiB) and hard (6.0 GiB) → classify soft.
+        with self._patch_psutil(rss_bytes=100 * 1024**2, avail_bytes=8 * 1024**3), \
+                patch.object(_memguard, "_mps_driver_bytes", return_value=int(5.3 * 1024**3)):
+            level, _, _ = _memguard.memory_pressure()
+        assert level == "soft"
```

(Existing `TestCheckAndMaybeExit` tests were also extended with
`patch.object(_memguard, "_mps_driver_bytes", return_value=0)` so that a real
MPS pool on the host — the dev laptop — cannot leak into the deterministic
test outcomes. This is the same contract as the psutil mock already in place.)

## pytest last 20 lines

```
tests/test_memguard.py::TestGetLimits::test_defaults PASSED              [  5%]
tests/test_memguard.py::TestGetLimits::test_env_overrides PASSED         [ 11%]
tests/test_memguard.py::TestPauseDaemon::test_returns_false_on_econnrefused PASSED [ 16%]
tests/test_memguard.py::TestPauseDaemon::test_returns_true_on_200 PASSED [ 22%]
tests/test_memguard.py::TestPauseDaemon::test_returns_false_on_other_url_error PASSED [ 27%]
tests/test_memguard.py::TestPauseDaemon::test_returns_false_on_generic_exception PASSED [ 33%]
tests/test_memguard.py::TestMemoryPressure::test_ok_when_well_below_thresholds PASSED [ 38%]
tests/test_memguard.py::TestMemoryPressure::test_soft_when_rss_at_soft PASSED [ 44%]
tests/test_memguard.py::TestMemoryPressure::test_soft_when_avail_low PASSED [ 50%]
tests/test_memguard.py::TestMemoryPressure::test_hard_when_rss_at_hard PASSED [ 55%]
tests/test_memguard.py::TestMemoryPressure::test_hard_when_avail_critical PASSED [ 61%]
tests/test_memguard.py::TestMemoryPressure::test_mps_pool_triggers_hard_even_when_rss_low PASSED [ 66%]
tests/test_memguard.py::TestMemoryPressure::test_mps_pool_soft_intermediate PASSED [ 72%]
tests/test_memguard.py::TestCheckAndMaybeExit::test_ok_returns_ok_no_compact PASSED [ 77%]
tests/test_memguard.py::TestCheckAndMaybeExit::test_soft_calls_compact_then_sleeps_when_still_soft PASSED [ 83%]
tests/test_memguard.py::TestCheckAndMaybeExit::test_hard_exits_via_sys_exit_0 PASSED [ 88%]
tests/test_memguard.py::TestFreeMemory::test_runs_gc_collect PASSED      [ 94%]
tests/test_memguard.py::TestFreeMemory::test_safe_when_torch_missing PASSED [100%]

============================== 18 passed in 1.41s ==============================
```

18/18 green = 16 pre-existing + 2 new.
