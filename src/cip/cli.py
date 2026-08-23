"""Command-line entry point: `python -m cip <command>`."""

from __future__ import annotations

import argparse
import json
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
    print("5. RED TEAM: untrusted transcripts vs the policy boundary")
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

    p = sub.add_parser("demo", help="end-to-end walkthrough")
    p.add_argument("--calls", type=int, default=4000)
    p.add_argument("--day", **day)
    p.add_argument("--workers", type=int, default=1)
    p.set_defaults(func=_cmd_demo)

    args = parser.parse_args(argv)
    print(f"[extractor backend: {CONFIG.extractor}]", file=sys.stderr)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
