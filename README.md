# FreightTiger — Smart Shipping Cost Watchdog

## Problem statement

FreightTiger needs an assistant that watches weekly shipping costs per route and spots
cost rises that look out of the ordinary — then explains each flag in plain English,
grounded only in a small set of supplied context notes. The system must never invent an
explanation: no supporting note means "unexplained", not a confident guess.

This repository contains a complete, tested, reproducible solution to the case study:
a deterministic Python/pandas pipeline that aggregates shipment records into weekly
route costs, compares them against two baselines, flags anomalies, retrieves only
genuinely applicable context notes, and emits `output.csv` in the exact supplied
contract format.

## Solution architecture

```
shipment_records.csv ──► load & validate ──► weekly aggregation ──► baselines ──► anomaly flags
context_notes.csv   ──► classify (evidence? scope? window?) ──────────────► note retrieval ──► reason
                                                                                          │
                                                                                          ▼
                                                                                    output.csv
```

Five stages, each a pure function in `solution.py`:

1. **Load & validate** — parse `shipment_date` (invalid dates coerced and dropped,
   counted), reject rows with missing required fields, build `route = origin-destination`.
2. **Weekly aggregation** — assign each shipment to its Monday-start week, group by
   `route`, `route_type`, and `week_of`, compute
   `cost_per_tonne_km = sum(freight_cost_inr) / sum(quantity_tonnes * distance_km)`.
   Groups with a zero tonne-km denominator are dropped, never fabricated.
3. **Baselines** — trailing-8-week own-history average (strictly prior weeks, all
   available weeks if fewer than eight) and same-week peer average over other routes
   with the same `route_type` (the route itself excluded).
4. **Anomaly detection** — flag when cost rises at least +10% vs own history or at
   least +15% vs peers (threshold rationale below).
5. **Evidence retrieval & explanation** — deterministic, rule-based note matching on
   route scope, effective date window, and evidence polarity; plain-English reasons
   that cite the exact note ID and state the verdict and numbers.

No LLM, no vector database, no randomness: the pipeline is fully deterministic.

## Prerequisites

- Python 3.10+
- pandas (>= 1.5)

```bash
pip install -r requirements.txt
```

## Commands

Run the solution (writes `output.csv` and `run_metrics.json`):

```bash
python solution.py
```

Run the full evaluation harness — 49 tests, 3-run reproducibility check with SHA-256
comparison, output schema validation — and write `evaluation_report.json`:

```bash
python evaluate.py
```

Run only the test suite:

```bash
python -m unittest discover -s tests
```

## Inputs

| File | Description |
|---|---|
| `shipment_records.csv` | 2,940 shipment records, 2024-01-01 to 2025-12-28, 7 routes, 3 route types. |
| `context_notes.csv` | 10 notes (N001–N010): fuel price changes, weather disruption, festival surcharge, toll update, plus notes that explicitly report no cost impact. |
| `sample_output_format_v2.csv` | The output contract: exact column names, order, and verdict conventions. |

Note: the supplied sample file itself has a formatting quirk — one `reason` field
contains an unquoted comma, so that row physically has 9 fields instead of 8. The
contract is the 8-column header; the solution emits properly quoted CSV (commas in
explanations are escaped, not bare).

## Output

`output.csv` — 728 rows (7 routes x 104 weeks), sorted by `week_of`, then `route`:

| Column | Meaning |
|---|---|
| `route` | `origin-destination` identifier. |
| `week_of` | Monday of the shipment week, `YYYY-MM-DD`. |
| `cost_per_tonne_km` | Weekly aggregate, two decimals. |
| `vs_own_history` | Percentage vs trailing-8-week own average, e.g. `+35.5% vs this route's past average`; empty when no history exists. |
| `vs_similar_routes` | Percentage vs same-week peer average, e.g. `+21.0% vs similar-length routes this week`; empty when no peer group exists. |
| `flagged` | `Yes` (anomaly, unexplained), `No (justified)` (anomaly with a matching note), or `No` (no anomaly). |
| `matched_note_id` | Exact note ID when justified, otherwise empty. |
| `reason` | Verdict, numbers, and — when justified — the note ID, its date, scope, and content. |

## Aggregation formula and baselines

Weekly cost per tonne-km per route:

```
cost_per_tonne_km = sum(freight_cost_inr) / sum(quantity_tonnes * distance_km)
```

Weeks run Monday–Sunday; `week_of` is that week's Monday date.

**Baseline 1 — own history:** the arithmetic mean of the route's weekly
cost per tonne-km over the trailing eight available weeks strictly before the
current week (no look-ahead). If fewer than eight prior weeks exist, all
available prior weeks are used. Never padded or extrapolated.

**Baseline 2 — similar routes:** the arithmetic mean, in the same week, of the
cost per tonne-km across all *other* routes sharing the same supplied
`route_type` (Short / Medium / Long). The route is excluded from its own peer
average.

Percentage difference against each baseline is computed only when the baseline
exists; missing baselines stay empty rather than becoming invented zeros.

## Threshold rationale and limitations

The brief does not prescribe numerical thresholds. Chosen thresholds, applied to
the *rising* direction only:

- **Own history: +10%.** A double-digit week-over-week rise against the route's
  own trailing average is material for freight rates and is the level at which
  manual review starts to pay off.
- **Similar routes: +15%.** A 15% premium over same-week peers indicates a
  route-specific issue rather than a network-wide condition; peer comparison can
  trigger a flag independently of own history (e.g. the sample's Mumbai-Pune
  2025-09-15 row: +9.2% vs own history but +23.6% vs peers → flagged).

Limitations:

- Thresholds are fixed and symmetric across route types; no seasonal adjustment.
- Own history is a plain mean of the last eight observed weeks, so a gradual
  drift is absorbed into the baseline over time (by design — the peer
  comparison catches divergence from the network).
- With one or two peers per route type, the peer average is sensitive to the
  peer's own anomalies.

## Evidence retrieval and anti-hallucination policy

`context_notes.csv` is the sole source of contextual justifications. The retriever
is a small deterministic rule engine — no vector database, no paid LLM — because
the note set is tiny, and auditability matters more than sophistication here.

Each note is classified once, at load time:

1. **Evidence polarity.** Notes that explicitly report stable conditions, normal
   operations, improved roads, no significant cost impact, unchanged rates, or
   out-of-scope routes are marked non-evidence and can never justify a flag.
   In this dataset: N004 (toll plaza, "affected routes are not part of this
   dataset"), N005 ("costs were not significantly affected"), N006 ("remained
   stable ... no major disruptions"), N007 ("improved road conditions"), N008
   ("no significant disruptions ... remained normal"), N009 (recovery: "returned
   to normal conditions"), N010 ("without a rate change").
   Only N001 (flooding), N002 (festival surcharge), and N003 (diesel price rise)
   are positive cost-increase evidence.
2. **Effective window.**
   - N001: explicit range parsed from the text — 2025-02-24 through 2025-03-08.
     The weeks of 2025-02-24 and 2025-03-03 overlap it; the week of 2025-03-10
     does not. The recovery note N009 (2025-03-17) never justifies.
   - N002: a one-week festival surcharge — the week of 2025-01-20 only.
   - N003: "starting this week" with no stated end. The note is bounded to an
     explicit 8-week window (2025-05-05 through 2025-06-29, i.e. weeks of May 5
     to June 23) rather than extended indefinitely. This bound is an explicit
     implementation heuristic, not a fact from the source: the note states no
     duration. The rationale is that a one-time level shift is absorbed into
     the trailing eight-week own-history baseline after eight weeks, so the
     note no longer explains an own-history anomaly beyond that point; and the
     supplied sample confirms the note must not justify September rows. The
     boundary is unit-tested (week of June 23 is inside, week of June 30 is
     outside).
3. **Scope.** Route-specific notes apply only to that route; `All Routes` notes
   apply to every route within their window.

A justified verdict requires all three: evidence polarity, matching route scope,
and week overlapping the effective window. When several notes match, a
route-specific note beats an `All Routes` note, then the earliest note ID wins.
`matched_note_id` is left blank whenever no genuinely applicable note exists.
Reasons quote the note's ID, date, scope, and content, and state that a note is
*evidence of a possible explanation, not proof that it fully accounts for the
measured increase*.

4. **Uniform-note scope rule.** A note whose scope is `All Routes` describes a
   network-wide condition that moves every route's cost roughly equally. Such a
   note can explain why a route is more expensive than *its own past* (an
   own-history trigger), but it **cannot** explain why the route diverges from
   *other routes in the same week* (a peer trigger) — the same network-wide
   change is already present in the peer average. Route-specific notes (N001,
   N002) can justify either trigger, because they explain why *this* route
   deviates from both its history and its peers. When an `All Routes` note is
   matched but rejected under this rule, the reason states that the note was
   considered and why it was rejected, so the decision is auditable rather
   than silent.

## Measured results (this dataset)

| Metric | Value |
|---|---|
| Shipment records processed | 2,940 (0 dropped: no invalid dates, no missing values, no zero denominators) |
| Route-week output rows | 728 |
| Flagged unexplained (`Yes`) | 24 |
| Justified (`No (justified)`) | 4 (N001 x2, N002 x1, N003 x1) |
| Unflagged (`No`) | 700 |
| LLM calls / input tokens / output tokens / cost | 0 / 0 / 0 / $0.00 |
| Execution time | ~1.5 s per full run |

Justified rows: Ahmedabad-Mumbai 2025-01-20 (N002, festival surcharge,
own-triggered +29.5%); Chennai-Bangalore 2025-02-24 and 2025-03-03 (N001,
flooding, own-triggered); Mumbai-Delhi 2025-05-12 (N003, diesel, own-triggered
+11.6%). Mumbai-Pune 2025-06-23 was previously justified by N003 but is now
correctly unexplained: its flag was peer-triggered (+20.6% vs peers, only +6.2%
vs own history), and a uniform nationwide diesel note cannot explain a
route-specific divergence from same-week peers.

The three sample-contract rows reproduce exactly: Delhi-Jaipur 2024-11-11
(4.17, +35.5%, +21.0%, `Yes`), Ahmedabad-Mumbai 2025-01-20 (3.29, +29.5%, +22.5%,
`No (justified)`, N002), Mumbai-Pune 2025-09-15 (3.98, +9.2%, +23.6%, `Yes`).

## Reproducibility

Three consecutive runs on the unchanged input files produce byte-identical
`output.csv` (verified by SHA-256):

```
3cf605deeb2f31c674e608bd27ed9d69df9b6acf76e8a955df3e43b381901c38
3cf605deeb2f31c674e608bd27ed9d69df9b6acf76e8a955df3e43b381901c38
3cf605deeb2f31c674e608bd27ed9d69df9b6acf76e8a955df3e43b381901c38
identical: true
```

The pipeline uses no randomness, no timestamps from the wall clock in its
output, and no network access.

## Evaluation

`python evaluate.py` runs 49 tests covering: Monday week assignment; the
aggregation formula; no look-ahead in the eight-week baseline; fewer-than-eight
history behaviour; peer averages excluding self; missing history and missing
peer groups; threshold boundaries (±0.1 pp around both thresholds); route-
specific note matching; date-window matching including the flood and recovery
periods; rejection of all seven non-evidence notes; the uniform-note scope rule
(`All Routes` notes cannot justify peer-triggered flags, with an auditable
rejection reason); blank `matched_note_id` for unexplained anomalies; CSV
schema, column order, Monday-date format, and two-decimal cost format; and
byte-level reproduction of the three supplied sample rows.

Result: **49 run, 0 failures, 0 errors**; schema check valid; three-run
reproducibility identical. Full details in `evaluation_report.json`; runtime
and cost facts in `run_metrics.json`.

## Edge cases and known limitations

- **First observed week of each route** has no own history: `vs_own_history` is
  empty and the row cannot be flagged on that baseline (peer comparison still
  applies). Seven such rows exist (one per route).
- **Missing peer group** would leave `vs_similar_routes` empty; handled
  explicitly (all routes have peers in every week of this dataset).
- **Invalid dates / missing values / zero denominators** are dropped and counted
  in the metrics; none occur in the supplied data, but the code paths are unit
  -tested with synthetic input.
- **Notes near a flag in date but non-impactful** (N005, N006, N007, N008, N010)
  are rejected by the polarity rule, not by date proximity.
- **Uniform network-wide notes** (N003) cannot justify peer-triggered flags,
  because a change that applies to all routes equally does not alter relative
  same-week comparisons; this is enforced by the uniform-note scope rule and
  covered by regression tests.
- **Threshold boundary** uses `>=`: exactly +10% vs own history or +15% vs peers
  flags the row.
- **The diesel note window is a documented design choice** (8-week bound),
  not a fact from the data; it is the one place where the solution adds an
  explicit bound where the note states none.

## Engineering trade-offs (for the interview)

- **Rule-based retrieval over RAG/LLM.** With ten notes, a vector database or
  LLM adds cost, latency, and a hallucination surface without improving recall.
  The rule engine's decisions are exactly auditable — every justification can be
  traced to a scope check, a date-window check, and a polarity check. The PDF's
  stretch goal (plain-English Q&A) is downstream of the same structured results
  and could be layered on later without changing the numerical core.
- **Determinism over sophistication.** No wall-clock timestamps, no randomness,
  stable sorts everywhere: three runs are byte-identical, which the brief
  explicitly tests.
- **Separation of concerns.** Numerical aggregation, baseline computation, flagging,
  note retrieval, and prose generation are independent functions; only the final
  stage reads the flagged rows, so note logic can never move a number.
- **Formula-level fidelity.** The brief's formulas (Monday weeks, ratio of sums,
  trailing eight strictly-prior weeks, peer mean excluding self) are implemented
  literally rather than approximated, and verified against the supplied sample
  rows to the displayed precision.
- **Honest baselines.** Missing baselines stay empty; no zero-fill, no
  extrapolation, no padding of missing route-weeks — 728 rows is exactly the
  number of observed route-weeks in the data.
