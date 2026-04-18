"""Index builder package — split from the original monolithic ``scripts/build_index.py``.

The orchestrator entry point is :func:`build_index` (formerly ``main()``).
Chunkers and indexers are re-exported here so the ``scripts/build_index.py``
shim and the ``tests/test_chunking.py`` suite can import them by their
historical names.
"""

from ._common import (
    DB_DIR,
    DB_PATH,
    DICTIONARY_DIR,
    DOMAIN_REGISTRY_FILE,
    EXTRACTED_DIR,
    FEATURE_REPO,
    FLOWS_DIR,
    GOTCHAS_DIR,
    INDEX_FILE,
    MAX_CHUNK,
    MIN_CHUNK,
    PROVIDERS_DIR,
    RAW_DIR,
    REFERENCES_DIR,
    TASKS_DIR,
)
from .code_chunks import _smart_chunk_js, chunk_code
from .code_facts import extract_code_facts
from .config_chunks import chunk_env, chunk_json, chunk_yaml
from .cql_chunks import _parse_cql_values, chunk_cql_seeds
from .db import (
    _delete_lancedb_repo,
    create_db,
    delete_repo_chunks,
    delete_repo_data,
    reset_repo_all_layers,
)
from .detect import detect_file_type, detect_language
from .dispatcher import chunk_file
from .docs_chunks import (
    _TASK_SECTION_MAP,
    _detect_task_chunk_type,
    _flush_task_section,
    chunk_markdown,
    chunk_task_markdown,
)
from .docs_indexer import (
    _index_domain_registry_simple,
    _insert_domain_entries,
    index_dictionary,
    index_domain_registry,
    index_flows,
    index_gotchas,
    index_providers,
    index_references,
    index_tasks,
)
from .incremental import (
    compute_profile_docs_fingerprint,
    detect_changed_repos,
    get_current_sha,
    load_existing_shas,
)
from .orchestrator import build_index
from .proto_chunks import chunk_proto
from .raw_indexer import index_seeds, index_test_scripts
from .repo_indexer import index_repo

__all__ = [
    "DB_DIR",
    "DB_PATH",
    "DICTIONARY_DIR",
    "DOMAIN_REGISTRY_FILE",
    "EXTRACTED_DIR",
    "FEATURE_REPO",
    "FLOWS_DIR",
    "GOTCHAS_DIR",
    "INDEX_FILE",
    # Constants / paths
    "MAX_CHUNK",
    "MIN_CHUNK",
    "PROVIDERS_DIR",
    "RAW_DIR",
    "REFERENCES_DIR",
    "TASKS_DIR",
    "_TASK_SECTION_MAP",
    "_delete_lancedb_repo",
    "_detect_task_chunk_type",
    "_flush_task_section",
    "_index_domain_registry_simple",
    "_insert_domain_entries",
    "_parse_cql_values",
    "_smart_chunk_js",
    # Orchestrator
    "build_index",
    "chunk_code",
    "chunk_cql_seeds",
    "chunk_env",
    "chunk_file",
    "chunk_json",
    "chunk_markdown",
    # Chunkers
    "chunk_proto",
    "chunk_task_markdown",
    "chunk_yaml",
    "compute_profile_docs_fingerprint",
    # DB schema / cleanup
    "create_db",
    "delete_repo_chunks",
    "delete_repo_data",
    "detect_changed_repos",
    "detect_file_type",
    # Detection
    "detect_language",
    # Code facts
    "extract_code_facts",
    "get_current_sha",
    "index_dictionary",
    "index_domain_registry",
    "index_flows",
    "index_gotchas",
    "index_providers",
    "index_references",
    # Indexers
    "index_repo",
    "index_seeds",
    "index_tasks",
    "index_test_scripts",
    # Incremental
    "load_existing_shas",
    "reset_repo_all_layers",
]
