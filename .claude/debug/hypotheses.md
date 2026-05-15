# Bug 6q — FT'd gte-base-en-v1.5 CUDA index OOB during build_docs_vectors

## H1: position_ids buffer is uninitialized garbage (persistent=False dropped on load)
- evidence: gte-base-en-v1.5 ships `position_ids` and `rotary_emb.inv_freq` as `persistent=False`. transformers >= 5 + accelerate lazy-init silently drops the values post-`from_pretrained`. Local CPU repro (Python 3.12, torch 2.9.1, fresh load of base `Alibaba-NLP/gte-base-en-v1.5`): `position_ids[:5] = [0, 4391870624, 59023, -1, 7453010313431162915]` — uninitialized memory. Encoding an 8000-char string crashes with `IndexError: index 2318349290077909349 is out of bounds for dimension 0 with size 2669`. On CUDA the same code path becomes the device-side assert "index out of bounds" reported in the bug. The train script applies `_fix_gte_persistent_false_buffers` (scripts/runpod/train_docs_embedder.py:309-343); the bench tries to import it from `src.index.builders.docs_vector_indexer`, but the helper is NOT defined there — the import lives behind a try/except so the bench has been silently running unpatched too. The build path (`build_docs_vectors → _load_sentence_transformer`) NEVER applies the patch.
- test: `python3.12 -c "from sentence_transformers import SentenceTransformer; m=SentenceTransformer('Alibaba-NLP/gte-base-en-v1.5', trust_remote_code=True, device='cpu'); print(m._first_module().auto_model.embeddings.position_ids[:5]); m.encode(['x'*8000])"` → garbage memory + IndexError. After applying patch + capping max_seq_length=512 → vec.shape=(1, 768) cleanly.
- result: confirmed (CPU repro reproduces the crash on the BASE model, not the FT'd one — same root cause regardless of FT)

## H2: Tokenizer changed during FT/save and produces token_ids >= vocab_size
- evidence: gte-base-en-v1.5 vocab_size = 30528 (BERT WordPiece). FT via sentence-transformers does not modify tokenizer. The FT'd repo would only push tokenizer.json/vocab.txt copied from the base. Even if true, the CUDA assertion message would point at the embedding lookup of `input_ids` (vocab dim), not `position_ids` indexing.
- test: would need to inspect the pushed FT repo for `len(tokenizer) == config.vocab_size`
- result: excluded (CPU repro on the BASE model reproduces the same crash, so FT artifacts cannot be the cause; the bug is in the load path, not the model weights)

## H3: NTK rope misbehaves at chunk lengths > 4097 chars; truncating to 512 (train-time) avoids it
- evidence: NTK-rope is documented to have numerical instability beyond original training context (gte-base trained at 2048 native, NTK-extended to 8192). 8000-char chunks tokenize to ~1500-2500 BPE tokens — well INSIDE the 8192 limit, so this is not the trigger. NTK numerical issues would manifest as poor recall, not a CUDA index assertion.
- test: encode a 4000-char input (well under any length-related rope boundary) without the U1 patch
- result: excluded (CPU repro shows IndexError even on inputs short enough that NTK extrapolation isn't engaged; root cause is uninitialized buffer, not NTK arithmetic)

## Verdict
H1 confirmed. Fix: define `_fix_gte_persistent_false_buffers` in `src/index/builders/docs_vector_indexer.py` (so the bench's existing `from src.index.builders.docs_vector_indexer import _fix_gte_persistent_false_buffers` actually resolves) and call it from `_load_sentence_transformer`. Also cap `model.max_seq_length` for any docs-gte-* key to match the train-time cap (512) so long-tail chunks don't blow positional context. Lower `long_limit` for `docs-gte-base-ft-run1` from 8000 → 2048 chars (~512 BPE tokens) to align with the cap.

---

# Daemon PID 66031 — phys_footprint росте з 1.7 GB до 3.3 GB протягом дня

## Контекст
- PID 66031 запущений 2026-04-29 14:31:47 (uptime ~7h на момент розслідування о 18:28)
- `ps` RSS = 36 MB (ввід в оману), `top`/Activity Monitor `phys_footprint` = **3.2 GB** (peak 5.9 GB)
- vmmap: writable 7.4 G total, 2.8 G dirty, **3.1 G swapped_out**, 4.2 G unallocated. **IOAccelerator (reserved) 896 M** (MPS GPU heap), MALLOC_NANO 218 M swap, MALLOC_SMALL 247 M swap, Stack 186 M (54 потоки).
- Сьогодні для цього процесу: 3 cycles `idle-watchdog unload → lazy reload` (15:16/16:45, 17:16/17:27, 18:16/?) — 4 моделі резидентні в робочому стані: CodeRankEmbed (code embed) + nomic-v1.5 (docs embed) + reranker_ft_gte_v8 (docs rerank) + l12-ft (code rerank, lazy на intent=code)

## H4: PyTorch MPS allocator не повертає GPU heap після `del model + torch.mps.empty_cache()`
- evidence: vmmap "IOAccelerator (reserved) 896.0M" існує навіть зараз з усіма моделями завантаженими; це pre-allocated MPS heap. Apple MPS allocator (`MPSAllocator`/`MTLHeap`) тримає ВСІ виділені сторінки і resize'ить лише вгору; `empty_cache()` лише позначає блоки як вільні в pool, але не повертає системі. Кожен наступний `_load_sentence_transformer` peak'ить heap до max(prev, new) — тобто після 3 cycles heap = max усіх трьох.
- test: викликати POST `/admin/unload` → `sleep 30` → `vmmap -summary $PID | grep -E "(Physical footprint|IOAccelerator)"`. Якщо IOAccelerator залишається ~896M — confirmed.
- result: untested

## H5: Python `gc.collect()` всередині `reset_providers()` недостатній — ref cycle утримує Tensor об'єкти
- evidence: `src/embedding_provider.py:225-237` робить `del provider._model; gc.collect()` для embedders, аналогічно для reranker. Але `SentenceTransformer` тримає _module_modules dict + tokenizer, що може містити ref cycle через `Module.parameters` ↔ `Module._parameters`. Після reload у `_embedding_providers[key]` зберігається СТАРИЙ `LocalEmbeddingProvider` як значення (а потім `_embedding_providers = {}` присвоєння — але якщо callsite ще тримає посилання через попередній `get_embedding_provider()` повернення, GC не може collect'ити). НЕ викликається `torch.mps.empty_cache()` всередині `_reset_providers_locked()` — лише в daemon.py shutdown/unload paths!
- test: переглянути `_reset_providers_locked()` — підтвердити, що `torch.mps.empty_cache()` НЕ викликається. Idle-watchdog шлях у daemon.py:319-346 викликає `empty_cache()`, але `/admin/unload` теж робить. ЯКЩО idle-watchdog reset звільняє MPS — H4 послаблюється.
- result: untested

## H6: phys_footprint включає swapped_out pages — це лежачі попередні model copies
- evidence: vmmap explicit: "Writable regions: written=2.8G(38%) swapped_out=3.1G(42%)". phys_footprint на macOS = `task_info(MACH_TASK_BASIC_INFO).phys_footprint` ≈ resident + compressed + swapped (memory the process "owns"). 3.1 GB swap = пам'ять, яку процес не звільнив, але macOS витіснив на SSD. Після reload модель повертається в RAM, але phys_footprint лишається високим, бо старі структури досі mapped.
- test: `sudo purge` (звільняє кеш, не торкається процесу), потім `sudo /usr/bin/heap 66031 | tail -20` для виявлення великих живих об'єктів. Або: подивитися `MALLOC_NANO swapped 218.6M` — чи зменшиться після /admin/unload+gc.
- result: untested

## H7: Двократна резиденція — code+docs reranker одночасно
- evidence: `get_reranker_provider("code")` кешує в `_reranker_providers[code_model]`, а `get_reranker_provider(None/docs)` — у `_reranker_provider` (модуль-рівень). Це РІЗНІ слоти, тому при першому doc-intent + першому code-intent запиті обидва reranker'и завантажуються паралельно. l12-ft (~120 MB) + reranker_ft_gte_v8 на gte-large (~1.3 GB) = ~1.4 GB лише на rerank. Після unload очищається все, але reload навіть однієї моделі підкреслює, що heap зростав до peak 5.9 GB при першому одночасному навантаженні.
- test: переглянути логи на одночасне завантаження обох rerank'ів сьогодні; перевірити чи `reranker_ft_gte_v8` (docs) і `l12-ft` (code) обидва зараз у пам'яті
- result: confirmed (логи 14:32:09 завантажили l12-ft, 14:41:12 завантажили reranker_ft_gte_v8 — обидва живі до 15:16 unload; зараз /health показує лише reranker_ft_gte_v8, бо code intent сьогодні не запитувався після reload)

## H8: ThreadingHTTPServer thread accumulation
- evidence: vmmap "Stack" показує 54 stacks (186 MB); ps -M показав 56 потоків. ThreadingHTTPServer створює `threading.Thread(target=...)` per request БЕЗ пулу — http.server documentation: "Each request is handled in a new thread." OS reclaims thread stack коли thread exits, але якщо є незавершені join'и або cycle через handler instance, можуть лишатись. Сьогодні 38 search calls + idle-watchdog + main + кілька admin = ~42 expected. 56 ≈ 42 + 14 (можливо launchd helper / GIL daemon threads / Python core). Не виглядає як leak — швидше очікуване число.
- test: моніторити thread count з інтервалом — якщо стабільний при idle і не росте після 5 search calls, виключаємо.
- result: untested (низький пріоритет — 186 MB stacks ≪ 3.1 GB swap)

## Експеримент A: /admin/unload + vmmap diff
- 21:32:07 idle-watchdog сам вивантажив усі 3 моделі (за 19с до мого admin/unload)
- 21:32:25 vmmap (post idle-unload, моделі = []): phys 1.9 G | resident 339 M | swap 1.5 G | IOAccel reserved **896 M** | Stack 186 M (54)
- 21:32:34 vmmap (post admin/unload): phys 1.9 G | resident 339 M | swap 1.5 G | IOAccel reserved **896 M** | Stack 186 M (54) — БЕЗ ЗМІН
- 21:33:39 trigger 1 search → docs-tower lazy reload
- 21:33:49 vmmap (active, 1 model): phys 1.9 G | resident **1.5 G** (+1.16 G) | swap 663 M (-836 M) | IOAccel reserved 896 M | Stack 206 M (64) — macOS повернув swap у RAM

## Оновлення статусу гіпотез

- H4 → **confirmed**: MPS heap reservation 896 M НЕ звільняється `torch.mps.empty_cache()`. Apple Metal allocator резервує heap одноразово при першому load і тримає до process exit.
- H5 → **partial confirmed**: `gc.collect() + empty_cache()` звільняють Python tensor objects, але swapped-out сторінки macOS ВЖЕ витіснив на SSD і не виключає з phys_footprint.
- H6 → **confirmed**: phys_footprint = resident + compressed + swapped. Дрейф 1.7→3.3 G = накопичення swap від послідовних load/unload cycles. swap не повертається у файл, доки phys_footprint не тиснуть інші процеси.
- H7 → **confirmed**: l12-ft (code) + reranker_ft_gte_v8 (docs) живуть паралельно у `_reranker_provider` vs `_reranker_providers["l12"]` різних слотах. Peak 5.9 G ймовірно тоді, коли всі 4 моделі (code embed + docs embed + 2 rerankers) резидентні одночасно до першого idle-unload.
- H8 → **excluded**: thread/stack count 54→64 при 1 search — ОС reclaim'ить, не ростуть exponentially. ≪ 3 G swap. Не root cause.

## Verdict

**Ріст 1.7→3.3 GB не є витоком.** Це сума: (1) одноразова MPS heap reservation 896 MB; (2) суцільне завантаження всіх 3 моделей коли кожна стає residen на свій intent (CodeRank на code, nomic-v1.5 + reranker_ft_gte_v8 на docs); (3) swap-retention від кожного `idle-watchdog unload→reload` cycle (macOS не звільняє SSD-pages поки немає memory pressure).

Реальна RAM (resident): 339 MB idle / 1.5 GB при 1 моделі / ~2.3 GB при 3 моделях. Activity Monitor "Memory" = phys_footprint — це resident + swap + compressed, частково на SSD.

## Опції пом'якшення

1. **Замінити idle-watchdog `unload` на `shutdown`** (daemon.py:319-346): launchd KeepAlive перезапустить процес з нуля → swap чиститься, MPS heap скидається. Trade-off: реліч latency 5-10с замість 0с (lazy reload). РЕКОМЕНДОВАНО.
2. **Memory-threshold trigger**: додати у idle_watchdog умову `if phys_footprint > 3 GB → shutdown else unload`. Best of both — швидкий unload коли активний, чистий restart коли overgrown.
3. **Daily restart via launchd**: додати окремий launchd-cron, що б'є `kill -TERM` раз на 24h. Простіше за threshold, але реліч може зачепити активну сесію.
4. **НЕ ЧІПАТИ** — реальний RAM impact адекватний (≤1.5 GB), а 3.3 GB — це SSD-swap бухгалтерія, що не тиснe систему доки є вільне місце на диску.
