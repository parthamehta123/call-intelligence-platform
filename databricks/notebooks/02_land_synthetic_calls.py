# Databricks notebook source
# MAGIC %md
# MAGIC # 02 · Land synthetic calls
# MAGIC
# MAGIC Writes a synthetic day into the raw volume so the pipeline can be
# MAGIC exercised end to end before real call data is wired up.
# MAGIC
# MAGIC The generator deliberately includes the four things that break naive
# MAGIC pipelines: overwhelming small talk, one defect reported thousands of
# MAGIC times, flatly contradictory claims, and prompt injection plus
# MAGIC credentials inside customer speech.
# MAGIC
# MAGIC **Not part of the scheduled job** — run it by hand.

# COMMAND ----------

dbutils.widgets.text("catalog", "cip_dev")
dbutils.widgets.text("schema", "call_intelligence")
dbutils.widgets.text("volume", "raw_calls")
dbutils.widgets.text("day", "2026-08-22")
dbutils.widgets.text("calls", "4000")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
volume = dbutils.widgets.get("volume")
day = dbutils.widgets.get("day")
calls = int(dbutils.widgets.get("calls"))

raw_root = f"/Volumes/{catalog}/{schema}/{volume}"

# COMMAND ----------

import json
import os
import shutil
import tempfile

os.environ["CIP_DATA"] = tempfile.mkdtemp()

from cip.config import CONFIG
from cip.generate import generate

CONFIG.lake = __import__("pathlib").Path(os.environ["CIP_DATA"]) / "lake"
local_dir = generate(n_calls=calls, day=day)
print("generated locally:", local_dir)

# COMMAND ----------

target = f"{raw_root}/date={day}"
dbutils.fs.mkdirs(target)

for path in sorted(local_dir.glob("*.jsonl")) + [local_dir / "_MANIFEST.json"]:
    shutil.copy(path, f"{target}/{path.name}")

display(dbutils.fs.ls(target))

# COMMAND ----------

print(json.dumps(json.loads((local_dir / "_MANIFEST.json").read_text())["counts"], indent=2))
