# Running this on Databricks

## What "the 10 TB claim is architectural" actually meant

The README says the throughput number was never benchmarked. Precisely:

* **What was true:** the pipeline stages are pure `Iterable -> Iterator`
  functions with no cross-record state, so they satisfy the contract Spark
  needs to run them one partition at a time.
* **What was *not* true:** that the whole pipeline lifted onto a cluster
  unchanged. Two stages held state over the entire day and genuinely had
  to be rewritten.

Here is the honest per-stage account, now that the port exists and runs.

| Stage | Ported how | Free? |
|---|---|---|
| 2 · preprocess (redact, segment) | `mapInPandas(preprocess_batches)` — same function body | **yes** |
| 3 · route (funnel) | fused into the extract pass | **yes** |
| 4 · extract | `mapInPandas(route_and_extract_batches)` — same `cip.pipeline.extract` | **yes** |
| routing decisions | `mapInPandas(route_decisions_batches)` → `route_decisions` | **yes** |

**Verified on dev**, run `Ra492dd2f21`: `injections_detected 57`,
`injections_inspection_only 14`, `observations 1195` — identical to the
local run. `route_decisions` holds 43 rows with `route_reason=both`
(inspected and extracted) and 14 with `route_reason=injection`
(inspected only), and the two new `segments` columns evolved additively.

The first attempt failed, and instructively. `Segment.injection_signatures`
is a list; `asdict()` put it straight into a `StringType` column. pandas
holds that as dtype `object` without complaint, and every local test
passed — Arrow rejected it only during serialisation on the cluster, four
minutes into the run. `_segment_row()` now performs the conversion in one
place, and `tests/test_security_channel.py` checks emitted rows against
the declared Spark types so the next such mismatch fails locally in half a
second instead.

`route_decisions` is a second pass rather than an extra output of the
extract stage, because `mapInPandas` yields exactly one schema. It records
one row per kept segment, and two consumers filter it: the security
channel is the rows with a non-empty signature, and the **exact metered
call count** is the rows with `reached_extraction`.

That second consumer exists because spend used to be summed from
observation rows, so a call returning no signal cost $0 in the report. On
the first full Claude day it hid 34 of 1225 calls. Measured on the
cluster: `kept 1239, to_model 1225, injections 57`. Locally an
injection the router forwards for inspection is recorded in the audit log;
an executor has no such log, so without this stage the cluster would drop
those segments silently — the identical failure the router override exists
to fix, one layer down. Re-running the router is pure CPU, which is why
paying for it twice is cheaper than threading a second output through
extraction.

It also runs over the **uncapped** segments, before `extract_limit` is
applied. That cap is a spend limit on model calls; letting it bound the
injection scan too would mean a cheap capped run inspected less of the day
than a full one.

| dedupe | **rewritten** — was an in-process `set`, now a shuffle | no |
| 5 · aggregate | **rewritten** — was a `defaultdict` over the whole day, now `groupBy/agg` | no |
| 6 · reconcile | unchanged, runs on the driver over ~12 rows | yes |
| 7 · declassify + 8 · publish | unchanged gate, storage swapped to Delta `MERGE` | yes |

The two rewrites are the honest part. A `set` lifted into `mapPartitions`
deduplicates *within a partition only* and silently misses the duplicates
that matter. A `defaultdict` over every observation is a driver-side
collect that is fine at 4,000 calls and fatal at 10 TB.

Because there are now two implementations of aggregation,
`tests/test_spark.py` pins them together — same input, identical
candidates and identical reconcile decisions. Two implementations without
that test would be a liability, not a feature.

### Also not free (found by actually running it)

* **`mapPartitions` is unavailable on Spark Connect.** `databricks-connect`
  13+ and serverless speak Connect, where `df.rdd` does not exist. Every
  stage uses `mapInPandas`, which has the same contract and works on
  classic, Connect and serverless alike.
* **pandas turns SQL `NULL` into float `NaN`.** A null `product_hint`
  arrived as `nan`, flowed through entity resolution as the product id and
  failed schema validation with *expected string, got float*. Every
  nullable column crossing the Arrow boundary goes through `_null()`.
* **Lazy re-evaluation after a write.** `deduped.count()` was computed
  *after* `record_seen` inserted this run's hashes, so the anti-join
  re-ran against the rows it had just written and reported `0`. The
  DataFrame is now cached and counted before the write.
* **Executors do not inherit the driver's `sys.path`.** Without the wheel
  attached as a job library the driver looks healthy and every task dies
  with `ModuleNotFoundError: cip`.
* **`catalog.json` lived outside the package.** In a wheel on a cluster
  there is no repo checkout, so the file did not exist, every product
  resolved to `None`, the funnel discarded 100% of traffic and the job
  "succeeded" having published nothing. It is now packaged, with the repo
  copy still winning locally.

## Do not install `pyspark` next to `databricks-connect`

Both ship a top-level `pyspark` package. Installing one over the other
leaves the loser's files orphaned in `site-packages`, and the result is an
import error deep inside `pyspark.sql.functions` that looks like a Spark
bug. Symptom:

```
ImportError: cannot import name 'AnalyzeArgument' from 'pyspark.sql.udtf'
```

Repair:

```bash
python3 -m pip uninstall -y pyspark
rm -rf "$(python3 -c 'import site;print(site.getsitepackages()[0])')/pyspark"
python3 -m pip install --force-reinstall --no-deps databricks-connect==14.3.13
```

So: the cluster and `databricks-connect` provide Spark; the `spark` extra
in `pyproject.toml` exists only for a **separate** local venv used to run
the parity tests.

```bash
python3 -m venv .venv-spark
.venv-spark/bin/pip install -e '.[spark,dev]'
.venv-spark/bin/python -m pytest tests/test_spark.py -q
```

`tests/test_spark.py` detects `databricks-connect` and skips rather than
hanging, so `make test` stays green in the normal environment.

## One-time workspace setup

```bash
databricks auth login --host https://<workspace>.cloud.databricks.com
```

Unity Catalog objects the job expects. **An admin creates the catalog in
the UI** — on Default Storage accounts `CREATE CATALOG` from SQL fails
without an explicit `MANAGED LOCATION`, and the job deliberately does not
hold that permission:

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<service-principal>`;
GRANT CREATE SCHEMA ON CATALOG <catalog> TO `<service-principal>`;
```

Before pointing the bundle at a catalog, prove it can actually be written
to — a catalog whose storage root has been deleted still lists fine:

```sql
CREATE TABLE IF NOT EXISTS <catalog>.<schema>._probe (x INT);
DROP TABLE <catalog>.<schema>._probe;
```

The production service principal holds exactly this — the list below is
what a real prod run actually needed, not what it looked like it would
need. An earlier version of this table named four tables; the job writes
nine, and it fails on the first one it cannot reach.

| Object | Grant | Why |
|---|---|---|
| catalog `cip` | `USE CATALOG` | reach anything at all |
| schema `cip.call_intelligence` | `USE SCHEMA` | same |
| `issues` | `SELECT, MODIFY` | the knowledge base |
| `evidence` | `SELECT, MODIFY` | append-only provenance |
| `review_queue` | `SELECT, MODIFY` | human queue |
| `policy_audit` | `SELECT, MODIFY` | decision log |
| `segments` | `SELECT, MODIFY` | silver, and replay without re-reading raw |
| `observations` | `SELECT, MODIFY` | materialised extraction output |
| `route_decisions` | `SELECT, MODIFY` | injection channel + call count |
| `seen_segments` | `SELECT, MODIFY` | global dedupe |
| `run_metrics` | `SELECT, MODIFY` | run history |
| the raw volume | `READ VOLUME` | never write |
| everything else | — | nothing |

`SELECT` alongside `MODIFY` is not redundant: the job reads back what it
wrote (`spark.table(...)`) to verify the extractor backend, count rows and
sum tokens. A writer that cannot read cannot check its own work.

Note what is **absent**: no `CREATE TABLE`, no `CREATE SCHEMA`, no
`CREATE CATALOG`. The tables are created by an admin before the first run,
out of band. The `setup` task's DDL is all `IF NOT EXISTS`, so it succeeds
as a no-op — but on a genuinely empty catalog it would fail, and that is
the correct trade: a nightly writer that can create tables can also create
one nobody is watching.

This is where the RBAC layer physically lives. The policy engine sits on
top of it; it does not replace it.

## Deploy

```bash
make bundle-validate        # databricks bundle validate -t dev
make bundle-deploy          # builds the wheel, uploads, creates the job
make bundle-run             # runs it once, streaming output
```

### Production

`prod` deliberately refuses to inherit anything from whoever runs the CLI.
Both of the following are required, and both exist for the same reason:

```bash
make prod-preflight SP=<application-id>          # does it exist?
make bundle-deploy TARGET=prod SP=<application-id>
make bundle-run    TARGET=prod SP=<application-id>
```

* **`run_as`** takes a service principal *application ID* (a UUID) — not an
  email, not a display name. A job owned by a person dies the day that
  person leaves. The default is the invalid sentinel
  `SET-ME-service-principal-application-id`. A variable with *no* default
  is not an option: Asset Bundles resolve every declared variable for every
  target, so it would break `validate` on dev too.

  Do **not** rely on the resulting error to explain itself. The sentinel
  appears in the deployment path, but the workspace reports
  `DIRECTORY_PROTECTED: Folder Users is protected` — which reads as a
  permissions problem and sends people to ask an admin for rights they do
  not need. `make bundle-deploy TARGET=prod` therefore refuses locally,
  before the wheel is even built.

* **`workspace.root_path`** is pinned to that service principal's home.
  Left unset, the path derives from the deploying user and two engineers
  produce two independent copies of the same job. The obvious alternative,
  `/Workspace/Shared`, is worse than untidy — the CLI flags it, and
  correctly: it is writable by every workspace user, so anyone could rewrite
  the notebooks and the wheel that the service principal then executes.
  That is a supply-chain hole running straight through the security
  boundary this project is built on.

Grant that service principal exactly the objects listed above and nothing
else.

#### Two things `validate` does not tell you

**It does not check that the service principal exists.** A typo'd or
retired application ID validates clean and fails at deploy, after the
wheel is built and uploaded. `make prod-preflight SP=<id>` asks the
workspace directly, which is the only way to find out cheaply.

**Until recently it did not check the prod target at all.** `bundle
validate` calls workspace `mkdirs` on the deployment root, and a root
under a nonexistent service principal's home fails with
`DIRECTORY_PROTECTED` *before* any job definition is read. With no service
principal in the workspace, every `validate -t prod` aborted on the first
API call — so the prod job definitions had never been checked, and an
error in them would have surfaced only during a real production deploy.
`root_path` is now the `prod_root_path` variable so the rest can be
validated without an identity:

```bash
make bundle-validate-prod    # Validation OK!
```

That passes. Everything in the prod target except the deployment root is
confirmed sound.

#### Bringing prod up from nothing

Done once, as an admin, in this order. Each step is here because it
actually blocked a deploy or a run.

**1 · The catalog.** `CREATE CATALOG cip` is rejected on Default Storage,
so it needs an explicit managed location inside an existing external
location:

```sql
CREATE CATALOG cip MANAGED LOCATION 's3://<bucket>/unity-catalog/<workspace-id>/cip';
CREATE SCHEMA cip.call_intelligence;
CREATE VOLUME cip.call_intelligence.raw_calls;
```

**2 · The tables**, created by an admin rather than the job — see the
grant table above for why the service principal has no `CREATE TABLE`.
Render them from `cip.spark.ddl.DDL`.

**3 · The service principal**, and the grants in the table above:

```bash
databricks service-principals create --display-name cip-prod-writer
```

**4 · The `servicePrincipal.user` role**, which is the step that is easy
to miss. Creating a service principal makes you its *manager*, and a
manager cannot bind it to a job:

```
Cannot bind the service principal provided in 'run_as' field to the job.
The user creating or updating the job must have 'servicePrincipal.user'
role on the service principal. (403 PERMISSION_DENIED)
```

Manager does not imply user. Add the role to the account rule set for that
principal (`PUT /api/2.0/preview/accounts/access-control/rule-sets`,
`roles/servicePrincipal.user`), keeping the existing manager rule.

**5 · Deploy and run:**

```bash
make prod-preflight SP=<application-id>
make bundle-deploy TARGET=prod SP=<application-id>
make bundle-run    TARGET=prod SP=<application-id>
```

#### Verified

`prod` has now been deployed and run end to end, `run_as` the service
principal, on the least-privilege grants above:

```json
{"day": "2026-08-24", "calls": 4000, "segments_landed": 3996,
 "injections_detected": 57, "injections_inspection_only": 14,
 "observations": 1195, "candidates": 8, "published": 6,
 "queued_for_review": 2}
security_checks: 10/10 blocked
```

Identical to dev on every figure. Read back from the `cip` tables rather
than taken from the run summary: `evidence` 1195, `observations` 1195,
`issues` 6, `review_queue` 2, and 57 injections in `route_decisions` — 43 `both`, 14
`injection`.

Two things to know about that run. `prod` takes `day: ""`, meaning
yesterday in UTC, computed on the cluster — unlike `dev`, which pins the
seeded day. A first prod run therefore looks for a date that has to exist
in the volume, and the failure is a bare `PATH_NOT_FOUND`. And the calls
used here are the synthetic day copied under that date, so the
`timestamp` inside each call disagrees with the partition it sits in; the
pipeline keys off the `day` parameter throughout, so this affects nothing
but is worth knowing before reading those rows.

`databricks.yml` builds the wheel from this repo and attaches it to every
task, which is what makes `import cip` work inside the Python workers.

The `dev` target is `mode: development`: resources are prefixed with your
username, the schedule is paused, and nothing collides with `prod`.

## Wiring up real Claude extraction

The default `rules` extractor runs offline and deterministically. For the
real path, store the key in a secret scope — never in the bundle:

```bash
databricks secrets create-scope cip
databricks secrets put-secret cip anthropic_api_key
```

Then in the cluster spec:

```yaml
spark_env_vars:
  CIP_EXTRACTOR: claude
  ANTHROPIC_API_KEY: "{{secrets/cip/anthropic_api_key}}"
```

Extraction uses `claude-opus-5` with `output_config.format` (JSON schema).
For the nightly sweep prefer `extract_claude_batch()` — the Batch API at
50% cost, results keyed by `custom_id`.

## What the real deployment changed

The job now runs green on Databricks serverless. Every number matches the
single-node run exactly:

```
calls 4000 · segments_landed 3997 · observations 1138 · evidence_rows 1138
candidates 12 · published 6 · queued_for_review 2 · rejected 4
audit_rows 22 · security_checks 10/10 blocked
```

Seven things broke between "runs on local Spark" and "runs on Databricks".
None were visible locally, which is the whole point of recording them.

**1 · Classic compute is unusable in this workspace.** Every job cluster
dies with `InvalidSubnetID.NotFound: subnet-<id> does not exist` — the
customer-managed VPC was torn down under the workspace. The job runs on
**serverless**, which uses Databricks' own network.

**2 · `CREATE CATALOG` is rejected on Default Storage accounts.**
`Metastore storage root URL does not exist` — a catalog needs an explicit
`MANAGED LOCATION`. The setup notebook no longer creates catalogs; it
asserts the catalog is visible and fails with a clear message otherwise.
That is also the correct governance posture: the job's identity should not
hold `CREATE CATALOG`.

**3 · A catalog can exist and still be dead.** A catalog resolved fine in
`SHOW CATALOGS`, but every write fails with `Bucket <name> does not
exist`. Its storage root was deleted along with the VPC. Probe a catalog
with a real write before trusting it.

**4 · `.cache()` is rejected on serverless** (`NOT_SUPPORTED_WITH_SERVERLESS:
PERSIST TABLE`). Dropping it naively would have re-run *extraction* three
times, once per downstream action. Each stage is now materialised as a
Delta table — bronze (raw volume) → silver (`segments`) → `observations` →
gold (`issues`, `evidence`). That is the medallion shape anyway, and it
buys replay: a bad extractor re-runs from silver without re-reading raw.

**5 · No filesystem work at import.** `audit = AuditLog()` ran a `mkdir` at
module import. Imported inside a UDF on an executor, that hit
`OSError: [Errno 30] Read-only file system` and failed the stage. The log
is now lazy, and a failed write is counted rather than fatal.

**6 · Positional argument drift.** `publish_candidates` grew a `day`
parameter; the caller still passed four positional args, so `config` bound
to `day` and Delta got
`replaceWhere: day = 'SparkConfig(catalog=...)'`. All cross-module calls
are keyword-only now.

**7 · A join reorders columns, and `insertInto` matches by position.**
`dedupe_across_days` joins on `(customer_id, content_hash)`; Spark hoists
join keys to the front. The write then shifted every column by two and
surfaced as a nonsense type error — *cannot cast `speaker_mix` STRING to
MAP*. `write_day_partition` now selects by the target table's column order.
`tests/test_spark.py` pins it.

Numbers 4–7 are code defects that local Parquet runs could not surface:
the Delta-only branch was never exercised, and the local warehouse had no
read-only filesystem.

## Sizing note

Still not run at 10 TB. What is now established: the logic is identical
between engines (parity tests), and the job completes end to end on real
Databricks serverless against Unity Catalog. Real sizing needs a measured
run — watch router precision first, since it prices every stage after it,
then extract-stage tokens per segment.
