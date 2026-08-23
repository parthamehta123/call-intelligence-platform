# Databricks notebook source
# MAGIC %md
# MAGIC # 05 · Security gate
# MAGIC
# MAGIC Runs the injection / exfiltration / poisoning scenarios **on the
# MAGIC cluster**, against the same policy engine the pipeline uses, and fails
# MAGIC the task if any attack executes.
# MAGIC
# MAGIC This is a job task rather than a notebook people remember to open: a
# MAGIC control nobody verifies is a control nobody has.

# COMMAND ----------

from cip.redteam import run

results = run()
blocked = [(s, o) for s, o in results if o.startswith("BLOCKED")]
executed = [(s, o) for s, o in results if not o.startswith("BLOCKED")]

for scenario, outcome in results:
    status = "BLOCKED " if outcome.startswith("BLOCKED") else "EXECUTED"
    print(f"[{status}] {scenario.name}")
    print(f"    control : {scenario.expected}")
    print(f"    result  : {outcome.split('-> ', 1)[-1][:160]}\n")

print(f"{len(blocked)}/{len(results)} attacks blocked by deterministic policy.")

# COMMAND ----------

if executed:
    raise AssertionError(
        "Security regression: these attacks were NOT blocked -> "
        + ", ".join(s.name for s, _ in executed))

dbutils.notebook.exit(f"{len(blocked)}/{len(results)} blocked")
