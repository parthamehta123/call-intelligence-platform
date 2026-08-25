"""Terminal labelling.

Deliberately spartan. The guideline is shown on every item because a
definition recalled from twenty items ago drifts, and the model's score and
prediction are never shown, because an annotator who knows what the system
thinks agrees with it.
"""

from __future__ import annotations

import time

from .pool import PoolItem, build_retrieval_pool, build_router_pool
from .store import Label, LabelStore

GUIDELINES = {
    "router": """Does the CUSTOMER say something about one of our products
that could update what we know about it -- a defect, a feature request, a
correction to the documentation, or praise for how it behaves?

  yes  a claim about the product, however casually worded
  no   greetings, billing, logistics, another vendor's kit, or the AGENT
       describing a defect rather than the customer reporting one
  skip genuinely unclear -- better skipped than guessed""",
    "retrieval": """Would showing this document help someone asking this
question?

  yes  it concerns the subject asked about, even partially
  no   a different subject, or the right product but the wrong attribute
  skip genuinely unclear""",
}


def label_session(kind: str, annotator: str, limit: int = 50,
                  store: LabelStore | None = None,
                  pool: list[PoolItem] | None = None) -> int:
    store = store or LabelStore()
    if pool is None:
        pool = (build_router_pool() if kind == "router"
                else build_retrieval_pool(_default_queries()))

    already = store.labelled_by(annotator)
    queue = [item for item in pool if item.item_id not in already][:limit]
    if not queue:
        print(f"nothing left to label for {annotator!r}")
        return 0

    print(f"\n{GUIDELINES[kind]}\n")
    print(f"{len(queue)} items. y = yes, n = no, s = skip, q = stop.\n")

    done = 0
    for index, item in enumerate(queue, start=1):
        print("-" * 68)
        print(f"[{index}/{len(queue)}]  {item.prompt()[:600]}")
        started = time.time()
        try:
            answer = input("  yes/no/skip? [y/n/s/q] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nstopping; labels so far are saved.")
            break
        if answer == "q":
            break
        if answer not in ("y", "n"):
            continue
        store.append(Label(item_id=item.item_id, kind=kind,
                           value=1 if answer == "y" else 0,
                           annotator=annotator,
                           seconds=round(time.time() - started, 2),
                           payload=item.payload))
        done += 1

    print(f"\nsaved {done} labels to {store.path}")
    return done


def _default_queries() -> list[str]:
    from ..eval.retrieval_eval import load_cases

    return [case.query for case in load_cases() if case.route == "rag"]


def export_router_labels(store: LabelStore | None = None) -> list[dict]:
    """Adjudicated labels in the shape `cip.eval.dataset` consumes.

    Items where annotators disagreed are excluded rather than resolved by
    majority: an eval built on the cases people could not agree about
    measures the ambiguity, not the router.
    """
    from .agreement import adjudicate

    store = store or LabelStore()
    contested = {row["item_id"] for row in adjudicate(store)}
    out = []
    for item_id, labels in store.by_item().items():
        if item_id in contested or labels[0].kind != "router":
            continue
        out.append({"customer": labels[0].payload.get("text", ""),
                    "label": labels[0].value,
                    "category": "human_labelled",
                    "annotators": sorted({l.annotator for l in labels})})
    return out
