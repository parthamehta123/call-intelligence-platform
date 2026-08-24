"""Router evaluation.

The relevance router prices every stage downstream of it: what it keeps is
what the LLM is paid to read, and what it drops is gone for good. Until it
is measured, no throughput or cost claim in this repo means anything.

The asymmetry matters more than the headline F1. A false positive costs
one extra inference call. A false negative loses a customer's report
permanently -- nothing later in the pipeline can recover a segment the
router discarded. So recall is the constraint and precision is the budget,
never the other way round.
"""

from .attribution_eval import AttributionReport, evaluate_attribution
from .dataset import EvalCase, load_generated, load_hard_cases
from .router_eval import Metrics, evaluate, sweep

__all__ = ["AttributionReport", "evaluate_attribution", "EvalCase", "load_generated", "load_hard_cases", "Metrics",
           "evaluate", "sweep"]
