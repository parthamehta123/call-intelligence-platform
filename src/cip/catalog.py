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


# Confidence for a generic category noun ("router", "console"). Deliberately
# well below the routing threshold's reach on its own: 0.45 * 0.60 = 0.27,
# so a generic term alone never admits a segment. It only carries one
# through in combination with actual problem language, which is the correct
# reading of the evidence -- "my home router from another vendor" mentions a
# router and reports nothing about ours.
GENERIC_CONFIDENCE = 0.60


# A version string is often the only product identifier a caller gives:
# "after installing firmware 7.2 the VPN keeps dropping" names no product at
# all. Same ambiguity rule as generic terms -- usable only while exactly one
# product ships that version. Confidence sits above a generic noun (a version
# is a much sharper signal) but below a curated alias.
VERSION_CONFIDENCE = 0.70


@lru_cache(maxsize=1)
def _version_index() -> dict[str, str]:
    """Version string -> product, keeping only versions unique catalog-wide."""
    owners: dict[str, list[str]] = {}
    for product in load_catalog():
        for version in product["versions"]:
            owners.setdefault(version, []).append(product["product_id"])
    return {v: pids[0] for v, pids in owners.items() if len(pids) == 1}


@lru_cache(maxsize=1)
def _generic_index() -> dict[str, str]:
    """Generic term -> product, keeping only terms unique across the catalog.

    A category noun is usable evidence exactly when the catalog contains one
    product of that category. Ship a second router and `router` stops being
    evidence -- this drops it automatically rather than silently resolving
    to whichever product was listed first.
    """
    owners: dict[str, list[str]] = {}
    for product in load_catalog():
        for term in product.get("generic_aliases", []):
            owners.setdefault(_norm(term), []).append(product["product_id"])
    return {term: pids[0] for term, pids in owners.items() if len(pids) == 1}


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
        # 3. generic category noun -> weak evidence, unambiguous terms only
        for term, owner in _generic_index().items():
            if owner == pid and f" {term} " in padded and GENERIC_CONFIDENCE > best[1]:
                best = (pid, GENERIC_CONFIDENCE)

        # 4. unambiguous version string -> identifies the product on its own
        for match in _VERSION_RE.finditer(text):
            owner = _version_index().get(match.group(1))
            if owner == pid and VERSION_CONFIDENCE > best[1]:
                best = (pid, VERSION_CONFIDENCE)

        # 5. fuzzy fallback -> flagged as low confidence, never auto-accepted
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
