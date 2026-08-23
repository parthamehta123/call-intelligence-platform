"""Product entity resolution.

Customers say "my wifi box", "the ap", "pulse seven". Embeddings alone
resolve these badly -- near-miss SKUs are semantically adjacent and get
confused. So the catalog does deterministic alias/SKU matching first and
only falls back to fuzzy scoring, which keeps the mapping auditable.
"""

from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from functools import lru_cache
from importlib import resources
from pathlib import Path

from .config import ROOT

# Resolution order matters for deployment. Inside a wheel on a Databricks
# cluster there is no repo checkout, so `ROOT/config/catalog.json` does not
# exist -- the packaged copy is the only one present. Locally the repo copy
# wins so the catalog can be edited without reinstalling.
CATALOG_ENV = "CIP_CATALOG_PATH"


def _catalog_path() -> Path:
    override = os.environ.get(CATALOG_ENV)
    if override:
        return Path(override)
    repo_copy = ROOT / "config" / "catalog.json"
    if repo_copy.exists():
        return repo_copy
    with resources.as_file(resources.files("cip.resources") / "catalog.json") as packaged:
        return packaged


CATALOG_PATH = None  # resolved lazily; see _catalog_path()

_VERSION_RE = re.compile(r"\b(?:v|version|firmware|release|build)?\s*(\d+\.\d+(?:\.\d+)?)\b", re.I)


@lru_cache(maxsize=1)
def load_catalog() -> list[dict]:
    return json.loads(_catalog_path().read_text())["products"]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower())


def resolve_product(text: str) -> tuple[str | None, float]:
    """Return (product_id, confidence). Exact alias/SKU beats fuzzy every time."""
    norm = _norm(text)
    padded = f" {norm} "
    best: tuple[str | None, float] = (None, 0.0)

    for product in load_catalog():
        pid = product["product_id"]
        # 1. exact SKU / canonical name -> highest confidence
        if f" {_norm(pid)} " in padded or _norm(product["canonical_name"]) in norm:
            return pid, 0.99
        # 2. curated alias -> high confidence
        for alias in product["aliases"]:
            if f" {_norm(alias)} " in padded:
                if 0.90 > best[1]:
                    best = (pid, 0.90)
        # 3. fuzzy fallback -> flagged as low confidence, never auto-accepted
        ratio = max(
            SequenceMatcher(None, _norm(alias), norm[:60]).ratio()
            for alias in product["aliases"]
        )
        if ratio > 0.62 and ratio * 0.7 > best[1]:
            best = (pid, round(ratio * 0.7, 3))

    return best


def resolve_version(text: str, product_id: str | None) -> str | None:
    match = _VERSION_RE.search(text)
    if not match:
        return None
    version = match.group(1)
    if product_id:
        known = {p["product_id"]: p["versions"] for p in load_catalog()}.get(product_id, [])
        # accept 7.2 when the catalog knows 7.2.13, but reject 99.9 outright
        if not any(v.startswith(version) or version.startswith(v) for v in known):
            return None
    return version
