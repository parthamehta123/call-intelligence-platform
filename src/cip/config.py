"""Runtime configuration.

Defaults are tuned for the local demo (thousands of calls). The comments
record what the same knob would be set to at 10 TB/day so the scaling
story stays attached to the code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("CIP_ROOT", Path(__file__).resolve().parents[2]))
DATA = Path(os.environ.get("CIP_DATA", ROOT / "data"))


@dataclass
class Config:
    # --- data lake layout -------------------------------------------------
    lake: Path = DATA / "lake"           # prod: s3://calls/raw/yyyy/mm/dd/
    warehouse: Path = DATA / "warehouse" # prod: Delta/Iceberg tables
    kb_path: Path = DATA / "kb.sqlite"   # prod: Postgres / Neptune / Neo4j
    audit_path: Path = DATA / "audit.log"

    # --- funnel -----------------------------------------------------------
    # Only segments above this score reach an LLM. At 10 TB/day this single
    # threshold is the difference between a $40k and a $2M daily inference bill.
    relevance_threshold: float = 0.35
    # Below this diarization confidence a customer turn is not trusted to be
    # the customer. Not a hard drop -- the observation is still emitted, with
    # confidence scaled by attribution, so weakly-attributed claims need more
    # corroboration to clear reconciliation rather than being silently lost.
    attribution_floor: float = 0.50
    max_segment_chars: int = 1200

    # --- reconciliation ---------------------------------------------------
    auto_accept_mentions: int = 8        # independent corroboration required
    auto_accept_customers: int = 5       # distinct customers, not distinct calls
    auto_accept_confidence: float = 0.72
    conflict_ratio: float = 0.30         # minority share that triggers review

    # --- retrieval --------------------------------------------------------
    # "hashed" keeps the demo dependency-free and the tests deterministic.
    # "sentence-transformers" runs a real encoder locally -- the only way the
    # hybrid-retrieval claim becomes testable, since a hashed bag-of-words
    # cannot disagree with BM25.
    embedder: str = os.environ.get("CIP_EMBEDDER", "hashed")
    embed_model: str = os.environ.get("CIP_EMBED_MODEL",
                                      "sentence-transformers/all-MiniLM-L6-v2")
    # Semantic-similarity floor for admitting a document the lexical
    # coverage check would reject. Only meaningful with a real encoder --
    # with the hashed backend, similarity is lexical overlap in disguise and
    # this floor is left effectively unused.
    dense_floor: float = float(os.environ.get("CIP_DENSE_FLOOR", "0.35"))
    # Abstention judge. "none" keeps tests deterministic and offline;
    # "claude" is the production path; "local" runs a small cached instruct
    # model so the mechanism can be measured without an API key.
    judge: str = os.environ.get("CIP_JUDGE", "none")
    judge_model: str = os.environ.get(
        "CIP_JUDGE_MODEL",
        "claude-opus-5" if os.environ.get("CIP_JUDGE") == "claude"
        else "Qwen/Qwen2.5-1.5B-Instruct")
    # Cross-encoder reranking. Off by default so the demo and tests need no
    # model; the heuristic reranker remains the fallback.
    reranker: str = os.environ.get("CIP_RERANKER", "none")
    reranker_model: str = os.environ.get(
        "CIP_RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
    rrf_k: int = 60                      # reciprocal-rank-fusion constant
    top_k: int = 5

    # --- extraction backend ----------------------------------------------
    # "rules" runs fully offline (default, deterministic, used by tests).
    # "claude" calls the Messages API with a constrained tool schema.
    extractor: str = os.environ.get("CIP_EXTRACTOR", "rules")
    claude_model: str = os.environ.get("CIP_MODEL", "claude-opus-5")

    # --- security ---------------------------------------------------------
    egress_allowlist: tuple[str, ...] = (
        "product-api.internal",
        "s3.amazonaws.com",
        "api.anthropic.com",
    )
    require_human_review: bool = True

    partitions: int = int(os.environ.get("CIP_PARTITIONS", "8"))

    def ensure_dirs(self) -> None:
        for p in (self.lake, self.warehouse, DATA):
            p.mkdir(parents=True, exist_ok=True)


CONFIG = Config()


@dataclass
class SparkConfig:
    """Cluster-side settings. Everything is overridable by env or job param.

    On Databricks the three-level Unity Catalog name is the unit of
    governance: grants, lineage and audit all hang off `catalog.schema`,
    which is also where the security story continues past this repo --
    the writer service's identity is a service principal with GRANT on
    exactly these tables and nothing else.
    """

    catalog: str = os.environ.get("CIP_CATALOG", "cip")
    schema: str = os.environ.get("CIP_SCHEMA", "call_intelligence")
    volume: str = os.environ.get("CIP_VOLUME", "raw_calls")
    # `delta` on Databricks; `parquet` lets the same code run on vanilla
    # PySpark locally, where MERGE is unavailable and publish falls back
    # to overwrite-by-partition.
    table_format: str = os.environ.get("CIP_TABLE_FORMAT", "delta")
    # Target ~128 MB per output file; at 10 TB/day this is the difference
    # between 80k sane files and 8M small ones that cripple the metastore.
    shuffle_partitions: int = int(os.environ.get("CIP_SHUFFLE_PARTITIONS", "200"))
    warehouse_dir: str = os.environ.get("CIP_WAREHOUSE_DIR", str(DATA / "spark-warehouse"))

    @property
    def namespace(self) -> str:
        return f"{self.catalog}.{self.schema}"

    def table(self, name: str) -> str:
        return f"{self.namespace}.{name}"

    @property
    def raw_path(self) -> str:
        return os.environ.get(
            "CIP_RAW_PATH", f"/Volumes/{self.catalog}/{self.schema}/{self.volume}")


SPARK = SparkConfig()
