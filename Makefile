.PHONY: demo run generate status redteam test ask clean wheel \
	eval-router eval-retrieval eval-retrieval-real eval-retrieval-judge eval-attribution \
	spark-setup spark-test spark-run bundle-validate bundle-deploy bundle-run

PY          := PYTHONPATH=src python3
SPARK_PY    := .venv-spark/bin/python
JAVA11      := $(shell /usr/libexec/java_home -v 11 2>/dev/null)
TARGET      ?= dev

# ---- single node -----------------------------------------------------------
demo:            ## full end-to-end walkthrough
	$(PY) -m cip demo --calls 4000

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
eval-retrieval-judge: ## same eval with the local LLM abstention judge
	CIP_JUDGE=local $(PY) -m cip eval-retrieval

eval-retrieval-real:
	HF_HUB_OFFLINE=1 CIP_EMBEDDER=sentence-transformers $(PY) -c \
	  "from cip import kb, retrieval; conn=kb.connect().__enter__(); conn.execute('UPDATE documents SET dirty=1'); conn.commit(); retrieval.refresh_index()"
	HF_HUB_OFFLINE=1 CIP_EMBEDDER=sentence-transformers $(PY) -m cip eval-retrieval

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

bundle-validate:
	databricks bundle validate -t $(TARGET)

wheel:
	rm -rf dist build src/*.egg-info
	python3 -m build --wheel

bundle-deploy: wheel
	databricks bundle deploy -t $(TARGET)

bundle-run:
	databricks bundle run daily_call_intelligence -t $(TARGET)

clean:
	rm -rf data metastore_db derby.log dist build
