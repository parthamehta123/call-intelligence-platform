.PHONY: demo run generate status redteam test ask clean wheel \
	eval-router eval-retrieval eval-retrieval-real eval-retrieval-judge \
	eval-identifiers eval-audio eval-rerank eval-groundedness graph \
	label-router label-retrieval label-status \
	eval-retrieval-judge-claude eval-attribution \
	spark-setup spark-test spark-run bundle-validate bundle-validate-prod \
	prod-preflight prod-guard bundle-deploy bundle-run

PY          := PYTHONPATH=src python3
SPARK_PY    := .venv-spark/bin/python
JAVA11      := $(shell /usr/libexec/java_home -v 11 2>/dev/null)
TARGET      ?= dev

# ---- single node -----------------------------------------------------------
demo:            ## full end-to-end walkthrough
	$(HF_ANON) $(PY) -m cip demo --calls 4000 --audio

generate:
	$(PY) -m cip generate --calls 4000

run:
	$(PY) -m cip run --workers 4

status:
	$(PY) -m cip status

redteam:
	$(PY) -m cip redteam

test:
	$(PY) -m pytest tests -q

ask:
	$(PY) -m cip ask "$(Q)"

eval-router:    ## measure the funnel: precision/recall/threshold sweep
	$(PY) -m cip eval-router

eval-retrieval: ## Recall@K / MRR / nDCG, leg ablation, routing, abstention
	$(PY) -m cip eval-retrieval

# Same eval against a real local encoder. HF_HUB_OFFLINE avoids a network
# round-trip for an already-cached model.
# HF_HUB_DISABLE_IMPLICIT_TOKEN: these models are public and need no auth.
# A stale token in ~/.cache/huggingface/token makes the hub return 401 where
# anonymous access succeeds -- which previously made the judge fail open on
# every document and report verdicts that were really just defaults.
HF_ANON := HF_HUB_DISABLE_IMPLICIT_TOKEN=1

eval-groundedness: ## is every claim in an answer backed by a citation?
	$(PY) -m cip eval-groundedness

label-router:   ## label router items (ANNOTATOR=name)
	@test -n "$(ANNOTATOR)" || (echo "usage: make label-router ANNOTATOR=your-name" && exit 1)
	$(PY) -m cip label router --annotator "$(ANNOTATOR)"

label-retrieval: ## label query/document pairs (ANNOTATOR=name)
	@test -n "$(ANNOTATOR)" || (echo "usage: make label-retrieval ANNOTATOR=your-name" && exit 1)
	$(PY) -m cip label retrieval --annotator "$(ANNOTATOR)"

label-status:   ## labelling coverage and inter-annotator agreement
	$(PY) -m cip label-status

graph:          ## traversal queries over canonical product state
	$(PY) -m cip graph

eval-identifiers: ## does lexical matching beat dense on near-miss versions?
	$(HF_ANON) CIP_EMBEDDER=sentence-transformers $(PY) -m cip eval-identifiers

eval-audio:     ## ASR / language ID / diarization accuracy on real audio
	$(HF_ANON) $(PY) -m cip eval-audio

# KMP_DUPLICATE_LIB_OK: faiss and torch each link an OpenMP runtime, which
# aborts the process on macOS when both load. Documented workaround.
eval-rerank:    ## retrieval quality with the cross-encoder reranker
	$(HF_ANON) KMP_DUPLICATE_LIB_OK=TRUE CIP_RERANKER=cross-encoder $(PY) -m cip eval-retrieval

eval-retrieval-judge: ## same eval with the local LLM abstention judge
	$(HF_ANON) CIP_JUDGE=local $(PY) -m cip eval-retrieval

# Requires ANTHROPIC_API_KEY in your environment. Never put the key in this
# file, in a shell literal, or in a commit -- export it in your own shell.
eval-retrieval-judge-claude: ## abstention eval with the claude-opus-5 judge
	@test -n "$$ANTHROPIC_API_KEY" || (echo "ANTHROPIC_API_KEY is not set; export it first" && exit 1)
	CIP_JUDGE=claude $(PY) -m cip eval-retrieval

eval-retrieval-real:
	$(HF_ANON) HF_HUB_OFFLINE=1 CIP_EMBEDDER=sentence-transformers $(PY) -c \
	  "from cip import kb, retrieval; conn=kb.connect().__enter__(); conn.execute('UPDATE documents SET dirty=1'); conn.commit(); retrieval.refresh_index()"
	$(HF_ANON) HF_HUB_OFFLINE=1 CIP_EMBEDDER=sentence-transformers $(PY) -m cip eval-retrieval

eval-attribution: ## did extracted evidence really come from the customer?
	$(PY) -m cip eval-attribution

# ---- spark / databricks ----------------------------------------------------
# Deliberately an isolated venv: installing plain pyspark next to
# databricks-connect breaks both. See docs/DATABRICKS.md.
spark-setup:
	python3 -m venv .venv-spark
	$(SPARK_PY) -m pip install -q --upgrade pip
	$(SPARK_PY) -m pip install -q -e '.[spark,dev]'

spark-test:
	JAVA_HOME=$(JAVA11) $(SPARK_PY) -m pytest tests/test_spark.py -q

spark-run:
	rm -rf data/spark-warehouse metastore_db derby.log
	JAVA_HOME=$(JAVA11) $(SPARK_PY) scripts/run_spark_local.py

SENTINEL := SET-ME-service-principal-application-id

bundle-validate:
	databricks bundle validate -t $(TARGET)

# Validate the prod target's job definitions without a service principal.
# `bundle validate` calls workspace mkdirs on the deployment root, so a
# root under a nonexistent SP home aborts before anything else is read --
# which meant prod was never structurally checked at all. Pointing the root
# at the caller's own home validates everything except the root itself.
bundle-validate-prod:
	databricks bundle validate -t prod \
	  --var="prod_root_path=/Workspace/Users/$$(databricks current-user me --output json | python3 -c 'import json,sys; print(json.load(sys.stdin)["userName"])')/.bundle/call-intelligence-platform/prod-validate"

# `bundle validate` does NOT check that the service principal exists, so a
# wrong application ID passes every local check and fails at deploy, after
# the wheel has been built and uploaded. Ask the workspace first.
prod-preflight:
	@test "$(SP)" != "" || { echo "usage: make prod-preflight SP=<application-id>"; exit 2; }
	@test "$(SP)" != "$(SENTINEL)" || { echo "SP is still the sentinel; pass a real application ID"; exit 2; }
	@databricks service-principals list --output json \
	  | python3 -c 'import json,sys; sps=json.load(sys.stdin) or []; \
	    match=[s for s in sps if s.get("applicationId")=="$(SP)"]; \
	    print("service principal found:", match[0].get("displayName","<no name>")) if match \
	    else (print("no service principal with applicationId $(SP) in this workspace.\n" \
	               "  databricks service-principals list   # shows what exists"), sys.exit(1))'

wheel:
	rm -rf dist build src/*.egg-info
	python3 -m build --wheel

# Listed BEFORE `wheel` so it fails before anything is built. Without the
# guard a prod deploy with the variable unset builds the wheel, uploads it,
# and fails against the workspace with `DIRECTORY_PROTECTED: Folder Users
# is protected` -- an error naming a permissions problem rather than the
# unset variable, which sends people to ask an admin for rights they do
# not need.
prod-guard:
	@if [ "$(TARGET)" = "prod" ] && { [ "$(SP)" = "" ] || [ "$(SP)" = "$(SENTINEL)" ]; }; then \
	  echo "prod deploys need the service principal's application ID:"; \
	  echo "  make prod-preflight SP=<application-id>   # check it exists first"; \
	  echo "  make bundle-deploy TARGET=prod SP=<application-id>"; \
	  exit 2; \
	fi

bundle-deploy: prod-guard wheel
	databricks bundle deploy -t $(TARGET) \
	  $(if $(SP),--var="run_as_service_principal=$(SP)",)

bundle-run:
	databricks bundle run daily_call_intelligence -t $(TARGET) \
	  $(if $(SP),--var="run_as_service_principal=$(SP)",)

clean:
	rm -rf data metastore_db derby.log dist build
