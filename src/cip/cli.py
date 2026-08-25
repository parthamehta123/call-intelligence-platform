"""Command-line entry point: `python -m cip <command>`."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import kb
from .config import CONFIG


def _cmd_generate(args) -> int:
    from .generate import generate
    path = generate(n_calls=args.calls, day=args.day)
    print(f"landed {args.calls} calls in {path}")
    return 0


def _cmd_run(args) -> int:
    from .pipeline.run import run_day
    stats = run_day(args.day, workers=args.workers)
    print(json.dumps(stats, indent=2))
    return 0


def _cmd_ask(args) -> int:
    from .agent import ask
    answer = ask(" ".join(args.question))
    print(f"[route: {answer.route}]\n{answer.answer}")
    if answer.citations:
        print("\ncitations:")
        for citation in answer.citations:
            print("  ", json.dumps(citation, default=str))
    if answer.blocked:
        print(f"\npolicy note: {answer.blocked}")
    return 0


def _cmd_status(args) -> int:
    issues = kb.query(
        "SELECT product_id, issue_key, severity, status, customers, mentions "
        "FROM issues ORDER BY customers DESC")
    queue = kb.query("SELECT product_id, issue_key, reason FROM review_queue "
                     "WHERE status='open'")
    print(f"{'PRODUCT':10} {'ISSUE':22} {'SEV':9} {'STATUS':10} {'CUST':>6} {'MENT':>6}")
    for row in issues:
        print(f"{row['product_id']:10} {row['issue_key']:22} {row['severity']:9} "
              f"{row['status']:10} {row['customers']:>6} {row['mentions']:>6}")
    print(f"\nhuman review queue: {len(queue)} open")
    for row in queue:
        print(f"  {row['product_id']}/{row['issue_key']}: {row['reason']}")
    return 0


def _cmd_evidence(args) -> int:
    rows = kb.query(
        "SELECT call_id, customer_id, region, quote, confidence, extractor "
        "FROM evidence WHERE product_id=? AND issue_key=? LIMIT ?",
        (args.product_id, args.issue_key, args.limit))
    print(f"evidence for {args.product_id}/{args.issue_key} "
          f"(showing {len(rows)}):\n")
    for row in rows:
        print(f"  {row['call_id']} [{row['region']}] conf={row['confidence']} "
              f"via {row['extractor']}\n    \"{row['quote'][:140]}\"")
    return 0


def _cmd_redteam(args) -> int:
    from .redteam import run
    results = run()
    blocked = 0
    for scenario, outcome in results:
        status = "BLOCKED" if outcome.startswith("BLOCKED") else "EXECUTED"
        blocked += status == "BLOCKED"
        print(f"\n[{status}] {scenario.name}")
        print(f"  transcript : {scenario.transcript[:90]}")
        print(f"  control    : {scenario.expected}")
        print(f"  result     : {outcome.split('-> ', 1)[-1][:170]}")
    print(f"\n{blocked}/{len(results)} attacks blocked by deterministic policy.")
    return 0 if blocked == len(results) else 1


def _cmd_eval_router(args) -> int:
    """Measure the funnel. Everything downstream is priced off this."""
    from .eval.dataset import load_generated, load_hard_cases
    from .eval.router_eval import report

    threshold = args.threshold if args.threshold is not None else CONFIG.relevance_threshold

    if args.set in ("hard", "both"):
        print(report(load_hard_cases(),
                     "HARD CASES - hand-written, tests generalisation", threshold))
        print()
    if args.set in ("generated", "both"):
        print(report(load_generated(day=args.day),
                     "GENERATED - labels exact, but shares an author with the router",
                     threshold))
    return 0


def _cmd_eval_attribution(args) -> int:
    from .eval.attribution_eval import evaluate_attribution
    print(evaluate_attribution(day=args.day).render())
    return 0


def _cmd_eval_retrieval(args) -> int:
    from .eval.retrieval_eval import report
    print(report(k=args.k))
    return 0


def _cmd_eval_audio(args) -> int:
    from .eval.audio_eval import evaluate_audio
    print(evaluate_audio().render())
    return 0


def _cmd_eval_identifiers(args) -> int:
    from .eval.identifier_eval import report
    print(report())
    return 0


def _cmd_graph(args) -> int:
    from .graph import render
    print(render())
    return 0


def _cmd_eval_groundedness(args) -> int:
    from .eval.groundedness_eval import evaluate_groundedness
    print(evaluate_groundedness().render())
    return 0


def _cmd_label(args) -> int:
    from .labeling.cli import label_session
    return 0 if label_session(args.kind, args.annotator, limit=args.limit) >= 0 else 1


def _cmd_label_status(args) -> int:
    from .labeling.agreement import adjudicate, agreement
    from .labeling.store import LabelStore

    store = LabelStore()
    print("=== labels ===")
    for key, value in store.summary().items():
        print(f"  {key:20} {value}")
    print()
    print(agreement(store).render())
    contested = adjudicate(store)
    if contested:
        print(f"\n  {len(contested)} items need adjudication "
              f"(excluded from exports until resolved)")
    return 0


def _cmd_audit(args) -> int:
    from .security.audit import audit
    for record in audit.tail(args.limit, event=args.event):
        print(json.dumps(record))
    return 0


def _cmd_demo(args) -> int:
    from .generate import generate
    from .pipeline.run import run_day
    from .agent import ask

    print("=" * 72)
    print("1. LANDING SYNTHETIC CALLS IN THE DATA LAKE")
    print("=" * 72)
    generate(n_calls=args.calls, day=args.day)

    print("\n" + "=" * 72)
    print("2. DAILY PIPELINE: preprocess -> funnel -> extract -> reconcile -> publish")
    print("=" * 72)
    stats = run_day(args.day, workers=args.workers)
    print(json.dumps(stats, indent=2))
    print(f"\nfunnel discarded {stats['funnel_reduction']:.0%} of segments before "
          f"any model inference")

    print("\n" + "=" * 72)
    print("3. CANONICAL PRODUCT STATE")
    print("=" * 72)
    _cmd_status(args)

    print("\n" + "=" * 72)
    print("4. SERVING: structured question -> SQL, descriptive question -> hybrid RAG")
    print("=" * 72)
    for question in ("How many customers reported each issue?",
                     "What is the status of the Pulse 7 overheating problem?"):
        answer = ask(question)
        print(f"\nQ: {question}\n[route: {answer.route}]\n{answer.answer[:400]}")

    print("\n" + "=" * 72)
    print("5. KNOWLEDGE GRAPH: traversals the relational store answers badly")
    print("=" * 72)
    from .graph import render as render_graph
    print(render_graph())

    print("\n" + "=" * 72)
    print("6. MEASUREMENT: every claim above has a number behind it")
    print("=" * 72)
    from .eval.attribution_eval import evaluate_attribution
    from .eval.dataset import load_generated, load_hard_cases
    from .eval.groundedness_eval import evaluate_groundedness
    from .eval.router_eval import evaluate

    for name, cases in (("generated", load_generated(day=args.day)),
                        ("hard cases", load_hard_cases())):
        m = evaluate(cases, CONFIG.relevance_threshold)
        print(f"  router / {name:<11} precision {m.precision:.3f}  "
              f"recall {m.recall:.4f}  kept {m.kept_fraction:.1%}")

    from .eval.retrieval_eval import score_mode, load_cases as load_retrieval
    r = score_mode(load_retrieval(), "hybrid", k=5)
    print(f"  retrieval            Recall@5 {r.recall:.3f}  MRR {r.mrr:.3f}  "
          f"nDCG@5 {r.ndcg:.3f}")

    g = evaluate_groundedness()
    print(f"  groundedness         {g.score:.3f} over {g.sentences} sentences, "
          f"{g.fabricated_citations} citations not retrieved")

    a = evaluate_attribution(day=args.day)
    print(f"  attribution          {a.contamination_rate:.2%} of "
          f"{a.observations} observations trace to a diarization flip "
          f"({a.agent_claims_present} agent restatements in the day)")

    if args.audio:
        print("\n" + "=" * 72)
        print("7. AUDIO INGESTION: real speech through ASR and diarization")
        print("=" * 72)
        # In a subprocess on purpose. FAISS and faster-whisper each link an
        # OpenMP runtime, and loading both in one process aborts on macOS.
        # KMP_DUPLICATE_LIB_OK would suppress it, but the OpenMP docs call
        # that unsafe and warn it can silently produce incorrect results --
        # not a trade worth making for a demo section. Separate processes
        # have one runtime each.
        import subprocess
        import sys

        completed = subprocess.run(
            [sys.executable, "-m", "cip", "eval-audio"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": "src"})
        print(completed.stdout.strip() or completed.stderr.strip()[-400:])

    print("\n" + "=" * 72)
    print(f"{8 if args.audio else 7}. RED TEAM: untrusted transcripts vs the policy boundary")
    print("=" * 72)
    return _cmd_redteam(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cip", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    day = dict(default="2026-08-22", help="partition date (YYYY-MM-DD)")

    p = sub.add_parser("generate", help="land synthetic calls in the data lake")
    p.add_argument("--calls", type=int, default=4000)
    p.add_argument("--day", **day)
    p.set_defaults(func=_cmd_generate)

    p = sub.add_parser("run", help="run the daily pipeline")
    p.add_argument("--day", **day)
    p.add_argument("--workers", type=int, default=1)
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("ask", help="ask the serving agent a question")
    p.add_argument("question", nargs="+")
    p.set_defaults(func=_cmd_ask)

    p = sub.add_parser("status", help="show canonical product state + review queue")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("evidence", help="show provenance behind one issue")
    p.add_argument("product_id")
    p.add_argument("issue_key")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=_cmd_evidence)

    p = sub.add_parser("redteam", help="run injection/exfiltration scenarios")
    p.set_defaults(func=_cmd_redteam)

    p = sub.add_parser("audit", help="tail the audit log")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--event", default=None)
    p.set_defaults(func=_cmd_audit)

    p = sub.add_parser("eval-router", help="measure the relevance funnel")
    p.add_argument("--set", choices=["hard", "generated", "both"], default="both")
    p.add_argument("--threshold", type=float, default=None)
    p.add_argument("--day", **day)
    p.set_defaults(func=_cmd_eval_router)

    p = sub.add_parser("eval-attribution", help="check evidence really came from the customer")
    p.add_argument("--day", **day)
    p.set_defaults(func=_cmd_eval_attribution)

    p = sub.add_parser("eval-retrieval", help="Recall@K / MRR / nDCG, ablation, routing")
    p.add_argument("--k", type=int, default=5)
    p.set_defaults(func=_cmd_eval_retrieval)

    p = sub.add_parser("eval-audio", help="ASR / language ID / diarization accuracy")
    p.set_defaults(func=_cmd_eval_audio)

    p = sub.add_parser("eval-identifiers",
                       help="does lexical matching beat dense on near-miss versions?")
    p.set_defaults(func=_cmd_eval_identifiers)

    p = sub.add_parser("eval-groundedness",
                       help="is every claim in an answer backed by a citation?")
    p.set_defaults(func=_cmd_eval_groundedness)

    p = sub.add_parser("label", help="label items for the eval set")
    p.add_argument("kind", choices=["router", "retrieval"])
    p.add_argument("--annotator", required=True,
                   help="who is labelling; two annotators give an agreement score")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=_cmd_label)

    p = sub.add_parser("label-status", help="coverage and inter-annotator agreement")
    p.set_defaults(func=_cmd_label_status)

    p = sub.add_parser("graph", help="traversal queries over canonical state")
    p.set_defaults(func=_cmd_graph)

    p = sub.add_parser("demo", help="end-to-end walkthrough")
    p.add_argument("--calls", type=int, default=4000)
    p.add_argument("--day", **day)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--audio", action="store_true",
                   help="include ASR + diarization (loads a speech model)")
    p.set_defaults(func=_cmd_demo)

    args = parser.parse_args(argv)
    print(f"[extractor backend: {CONFIG.extractor}]", file=sys.stderr)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
