"""Terminal labelling.

Deliberately spartan. The guideline is shown on every item because a
definition recalled from twenty items ago drifts, and the model's score and
prediction are never shown, because an annotator who knows what the system
thinks agrees with it.
"""

from __future__ import annotations

import sys
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


class LabellingAborted(RuntimeError):
    """Raised rather than finishing a session that recorded nothing."""


def label_session(kind: str, annotator: str, limit: int = 50,
                  store: LabelStore | None = None,
                  pool: list[PoolItem] | None = None,
                  ask=None) -> int:
    """Run an interactive labelling session.

    `ask` exists for tests. When it is None the session is interactive and
    refuses to run without a terminal -- see the note on the tty check.
    """
    interactive = ask is None
    if interactive and not sys.stdin.isatty():
        # A session driven by a non-terminal stdin reads "" for every item,
        # which the loop below used to treat as a skip. The result was 50
        # prompts printed in one burst, every judgement discarded, and
        # `saved 0 labels` reported as a normal finish. Refusing is the
        # only safe behaviour: there is no answer to record, and printing
        # the items anyway burns the pool's blindness for nothing.
        raise LabellingAborted(
            "labelling needs a terminal; stdin is not a tty. Run it directly:\n"
            f"  make label-{kind} ANNOTATOR=<name>")
    ask = ask or (lambda prompt: input(prompt))
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
    quit_early = False
    for index, item in enumerate(queue, start=1):
        print("-" * 68)
        print(f"[{index}/{len(queue)}]  {item.prompt()[:600]}")
        started = time.time()
        answer = _ask_until_valid(ask)
        if answer is None:                      # EOF, interrupt, or refusal
            print("\nstopping; labels so far are saved.")
            break
        if answer == "q":
            quit_early = True
            break
        if answer == "s":
            continue
        store.append(Label(item_id=item.item_id, kind=kind,
                           value=1 if answer == "y" else 0,
                           annotator=annotator,
                           seconds=round(time.time() - started, 2),
                           payload=item.payload))
        done += 1

    if not done and quit_early:
        # Deliberately stopping before the first answer is a choice, not a
        # malfunction. Reporting it as an abort would make a normal early
        # exit indistinguishable from the data loss below.
        print("\nstopped before labelling anything; nothing to save.")
        return 0

    if not done:
        # The failure this guards against reported success: every item
        # displayed, nothing recorded, exit 0. A labelling session that
        # captured no judgement has not happened.
        raise LabellingAborted(
            f"{len(queue)} items shown and 0 labels recorded. Nothing was "
            f"written to {store.path}.")

    print(f"\nsaved {done} labels to {store.path}")
    return done


VALID = ("y", "n", "s", "q")


def _ask_until_valid(ask, tries: int = 3) -> str | None:
    """One keystroke, or None to stop the session.

    Re-prompts instead of skipping. An unrecognised answer used to fall
    through to `continue`, so a mistyped key silently discarded that item's
    judgement and the annotator had no way to tell.
    """
    # The cap covers EVERY unrecognised answer, not only blank ones. Counting
    # blanks alone left a non-empty invalid token -- a stuck key, a driver
    # feeding a constant string -- re-prompting forever, which is a hang
    # rather than the clean abort this function exists to produce.
    rejected = 0
    while True:
        try:
            answer = ask("  yes/no/skip? [y/n/s/q] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if answer in VALID:
            return answer
        rejected += 1
        if rejected >= tries:
            print("  no usable answer after "
                  f"{tries} attempts; stopping.")
            return None
        if not answer:
            print("  (no answer read) y = yes, n = no, s = skip, q = stop")
        else:
            print(f"  {answer!r} is not one of y / n / s / q")


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
