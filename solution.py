from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
SHIPMENTS_CSV = BASE_DIR / "shipment_records.csv"
NOTES_CSV = BASE_DIR / "context_notes.csv"
OUTPUT_CSV = BASE_DIR / "output.csv"
RUN_METRICS_JSON = BASE_DIR / "run_metrics.json"

OWN_HISTORY_WEEKS = 8
OWN_HISTORY_THRESHOLD_PCT = 10.0
PEER_THRESHOLD_PCT = 15.0
DIESEL_ABSORPTION_WEEKS = 8

OUTPUT_COLUMNS = [
    "route",
    "week_of",
    "cost_per_tonne_km",
    "vs_own_history",
    "vs_similar_routes",
    "flagged",
    "matched_note_id",
    "reason",
]

MONTH_NUMBERS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

NON_EVIDENCE_MARKERS = (
    "not part of this dataset",
    "not significantly affected",
    "remained stable",
    "no major disruptions",
    "remained normal",
    "improved road conditions",
    "no significant disruptions",
    "returned to normal",
    "without a rate change",
)

EXPLICIT_RANGE_RE = re.compile(
    r"from\s+([a-z]+)\s+(\d{1,2})\s+to\s+([a-z]+)\s+(\d{1,2})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ContextNote:
    note_id: str
    date: pd.Timestamp
    applies_to: str
    text: str
    is_evidence: bool
    window_start: pd.Timestamp | None
    window_end: pd.Timestamp | None
    summary: str


@dataclass(frozen=True)
class ShipmentValidation:
    total_rows: int
    rows_used: int
    invalid_dates: int
    rows_with_missing_values: int
    zero_denominator_groups: int


def monday_of_week(date: pd.Timestamp) -> pd.Timestamp:
    return date - pd.Timedelta(days=date.weekday())


def load_shipments(path: Path) -> tuple[pd.DataFrame, ShipmentValidation]:
    raw = pd.read_csv(path)
    total_rows = len(raw)

    dates = pd.to_datetime(raw["shipment_date"], errors="coerce")
    invalid_dates = int(dates.isna().sum())

    required = ["origin", "destination", "route_type", "quantity_tonnes", "distance_km", "freight_cost_inr"]
    missing_mask = raw[required].isna().any(axis=1)
    rows_with_missing_values = int(missing_mask.sum())

    df = raw.loc[~missing_mask].copy()
    df["shipment_date"] = dates.loc[~missing_mask]
    df = df.dropna(subset=["shipment_date"])
    df["route"] = df["origin"] + "-" + df["destination"]
    df["week_of"] = df["shipment_date"].dt.normalize() - pd.to_timedelta(
        df["shipment_date"].dt.weekday, unit="D"
    )
    df["tonne_km"] = df["quantity_tonnes"] * df["distance_km"]

    validation = ShipmentValidation(
        total_rows=total_rows,
        rows_used=len(df),
        invalid_dates=invalid_dates,
        rows_with_missing_values=rows_with_missing_values,
        zero_denominator_groups=0,
    )
    return df, validation


def aggregate_weekly(shipments: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    df = shipments.copy()
    if "tonne_km" not in df.columns:
        df["tonne_km"] = df["quantity_tonnes"] * df["distance_km"]
    if "week_of" not in df.columns:
        dates = pd.to_datetime(df["shipment_date"], errors="coerce")
        df["week_of"] = dates.dt.normalize() - pd.to_timedelta(dates.dt.weekday, unit="D")
    grouped = (
        df.groupby(["route", "route_type", "week_of"], as_index=False)
        .agg(
            total_cost=("freight_cost_inr", "sum"),
            total_tonne_km=("tonne_km", "sum"),
            shipment_count=("shipment_id", "count"),
        )
    )
    valid = grouped["total_tonne_km"] > 0
    zero_denominator_groups = int((~valid).sum())
    grouped = grouped.loc[valid].copy()
    grouped["cost_per_tonne_km"] = grouped["total_cost"] / grouped["total_tonne_km"]
    return grouped, zero_denominator_groups


def add_baselines(weekly: pd.DataFrame) -> pd.DataFrame:
    weekly = weekly.sort_values(["route", "week_of"]).reset_index(drop=True)
    own_history_avg = []
    peer_avg = []
    for row in weekly.itertuples():
        hist = weekly.loc[
            (weekly["route"] == row.route) & (weekly["week_of"] < row.week_of), "cost_per_tonne_km"
        ].tail(OWN_HISTORY_WEEKS)
        own_history_avg.append(float(hist.mean()) if len(hist) else float("nan"))

        peers = weekly.loc[
            (weekly["route_type"] == row.route_type)
            & (weekly["week_of"] == row.week_of)
            & (weekly["route"] != row.route),
            "cost_per_tonne_km",
        ]
        peer_avg.append(float(peers.mean()) if len(peers) else float("nan"))

    weekly["own_history_avg"] = own_history_avg
    weekly["peer_avg"] = peer_avg
    weekly["vs_own_history_pct"] = (
        (weekly["cost_per_tonne_km"] - weekly["own_history_avg"]) / weekly["own_history_avg"] * 100
    )
    weekly["vs_peer_pct"] = (
        (weekly["cost_per_tonne_km"] - weekly["peer_avg"]) / weekly["peer_avg"] * 100
    )
    return weekly


def detect_anomalies(weekly: pd.DataFrame) -> pd.DataFrame:
    weekly = weekly.copy()
    own_flag = weekly["vs_own_history_pct"] >= OWN_HISTORY_THRESHOLD_PCT
    peer_flag = weekly["vs_peer_pct"] >= PEER_THRESHOLD_PCT
    weekly["is_flagged"] = own_flag | peer_flag
    return weekly


def classify_note(note_id: str, date: pd.Timestamp, applies_to: str, text: str) -> ContextNote:
    lowered = text.lower()
    is_evidence = not any(marker in lowered for marker in NON_EVIDENCE_MARKERS)
    window_start = None
    window_end = None
    if is_evidence:
        year = date.year
        match = EXPLICIT_RANGE_RE.search(text)
        if match:
            window_start = pd.Timestamp(
                year=year, month=MONTH_NUMBERS[match.group(1).lower()], day=int(match.group(2))
            )
            window_end = pd.Timestamp(
                year=year, month=MONTH_NUMBERS[match.group(3).lower()], day=int(match.group(4))
            )
        elif "temporary" in lowered:
            window_start = date
            window_end = date + timedelta(days=6)
        elif "starting this week" in lowered:
            window_start = date
            window_end = date + timedelta(weeks=DIESEL_ABSORPTION_WEEKS) - timedelta(days=1)
        else:
            window_start = date
            window_end = date + timedelta(days=6)
    summary = text.split(". ")[0].rstrip(".")
    return ContextNote(
        note_id=note_id,
        date=date,
        applies_to=applies_to,
        text=text,
        is_evidence=is_evidence,
        window_start=window_start,
        window_end=window_end,
        summary=summary,
    )


def load_notes(path: Path) -> list[ContextNote]:
    raw = pd.read_csv(path)
    notes = []
    for row in raw.itertuples():
        notes.append(
            classify_note(
                note_id=row.note_id,
                date=pd.Timestamp(row.date),
                applies_to=row.applies_to,
                text=row.note,
            )
        )
    return notes


def retrieve_note(route: str, week_of: pd.Timestamp, notes: list[ContextNote]) -> ContextNote | None:
    candidates = []
    for note in notes:
        if not note.is_evidence or note.window_start is None:
            continue
        if note.applies_to not in (route, "All Routes"):
            continue
        week_end = week_of + timedelta(days=6)
        if week_of <= note.window_end and week_end >= note.window_start:
            candidates.append(note)
    if not candidates:
        return None
    candidates.sort(key=lambda n: (n.applies_to != "All Routes", n.note_id))
    return candidates[0]


def format_pct(value: float) -> str:
    return "" if pd.isna(value) else f"{value:+.1f}%"


def comparison_phrase(value: float, label: str) -> str:
    return "" if pd.isna(value) else f"{value:+.1f}% vs {label}"


def build_reason(row, note: ContextNote | None, verdict: str) -> str:
    week_str = row.week_of.strftime("%Y-%m-%d")
    if verdict == "No":
        own_phrase = comparison_phrase(row.vs_own_history_pct, "own 8-week history")
        peer_phrase = comparison_phrase(row.vs_peer_pct, "similar-length routes this week")
        parts = []
        parts.append(own_phrase if own_phrase else "no own-history baseline yet (first observed week)")
        parts.append(peer_phrase if peer_phrase else "no peer routes of the same type that week")
        return "Cost within normal range: " + "; ".join(parts) + ". Not flagged."

    own_phrase = comparison_phrase(row.vs_own_history_pct, "own 8-week history")
    peer_phrase = comparison_phrase(row.vs_peer_pct, "similar-length routes this week")
    comparison = " and ".join(p for p in (own_phrase, peer_phrase) if p)
    if not comparison:
        comparison = "cost rise exceeds anomaly thresholds"

    if note is not None:
        return (
            f"Flagged anomaly ({comparison}). Matches note {note.note_id} "
            f"({note.date.strftime('%Y-%m-%d')}, applies to {note.applies_to}): {note.summary}. "
            "The note is evidence of a possible explanation, not proof that it fully accounts "
            "for the measured increase."
        )
    return (
        f"Flagged anomaly ({comparison}). No note in context_notes.csv provides a genuine "
        f"cost-rise explanation for {row.route} in the week of {week_str}; flagged for human review."
    )


def build_output_rows(weekly: pd.DataFrame, notes: list[ContextNote]) -> list[dict]:
    rows = []
    for row in weekly.itertuples():
        note = retrieve_note(row.route, row.week_of, notes) if row.is_flagged else None
        if not row.is_flagged:
            verdict = "No"
        elif note is not None:
            verdict = "No (justified)"
        else:
            verdict = "Yes"
        rows.append(
            {
                "route": row.route,
                "week_of": row.week_of.strftime("%Y-%m-%d"),
                "cost_per_tonne_km": f"{row.cost_per_tonne_km:.2f}",
                "vs_own_history": (
                    f"{row.vs_own_history_pct:+.1f}% vs this route's past average"
                    if not pd.isna(row.vs_own_history_pct)
                    else ""
                ),
                "vs_similar_routes": (
                    f"{row.vs_peer_pct:+.1f}% vs similar-length routes this week"
                    if not pd.isna(row.vs_peer_pct)
                    else ""
                ),
                "flagged": verdict,
                "matched_note_id": note.note_id if note is not None else "",
                "reason": build_reason(row, note, verdict),
            }
        )
    rows.sort(key=lambda r: (r["week_of"], r["route"]))
    return rows


def write_output(rows: list[dict], output_path: Path) -> None:
    pd.DataFrame(rows, columns=OUTPUT_COLUMNS).to_csv(output_path, index=False)


def run_pipeline(input_dir: Path = BASE_DIR, output_path: Path = OUTPUT_CSV) -> dict:
    start = time.perf_counter()
    shipments, validation = load_shipments(input_dir / "shipment_records.csv")
    notes = load_notes(input_dir / "context_notes.csv")
    weekly, zero_denominator_groups = aggregate_weekly(shipments)
    validation = ShipmentValidation(
        total_rows=validation.total_rows,
        rows_used=validation.rows_used,
        invalid_dates=validation.invalid_dates,
        rows_with_missing_values=validation.rows_with_missing_values,
        zero_denominator_groups=zero_denominator_groups,
    )
    weekly = add_baselines(weekly)
    weekly = detect_anomalies(weekly)
    rows = build_output_rows(weekly, notes)
    write_output(rows, output_path)
    elapsed = time.perf_counter() - start

    result = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    metrics = {
        "shipment_records_processed": validation.rows_used,
        "shipment_records_total_in_file": validation.total_rows,
        "invalid_date_rows_dropped": validation.invalid_dates,
        "rows_with_missing_values_dropped": validation.rows_with_missing_values,
        "zero_denominator_groups_dropped": zero_denominator_groups,
        "route_week_output_rows": len(rows),
        "flagged_unexplained_rows": int((result["flagged"] == "Yes").sum()),
        "justified_rows": int((result["flagged"] == "No (justified)").sum()),
        "unflagged_rows": int((result["flagged"] == "No").sum()),
        "llm_calls": 0,
        "llm_input_tokens": 0,
        "llm_output_tokens": 0,
        "estimated_model_cost_usd": 0.0,
        "pricing_assumptions": (
            "No LLM used. Deterministic rule-based implementation: 0 LLM calls, 0 input tokens, "
            "0 output tokens, 0 USD model cost. Local Python/pandas processing only."
        ),
        "execution_time_seconds": round(elapsed, 4),
        "thresholds": {
            "own_history_threshold_pct": OWN_HISTORY_THRESHOLD_PCT,
            "peer_threshold_pct": PEER_THRESHOLD_PCT,
            "own_history_weeks": OWN_HISTORY_WEEKS,
        },
    }
    return metrics


def main() -> None:
    metrics = run_pipeline()
    RUN_METRICS_JSON.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print(f"output.csv written: {metrics['route_week_output_rows']} route-week rows")
    print(
        "flags: "
        f"{metrics['flagged_unexplained_rows']} unexplained, "
        f"{metrics['justified_rows']} justified, "
        f"{metrics['unflagged_rows']} unflagged"
    )
    print(f"run_metrics.json written ({metrics['execution_time_seconds']}s)")


if __name__ == "__main__":
    main()
