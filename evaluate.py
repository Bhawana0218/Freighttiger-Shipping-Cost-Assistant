from __future__ import annotations

import hashlib
import io
import json
import sys
import time
import unittest
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

import solution
from solution import OUTPUT_COLUMNS, run_pipeline

EVALUATION_REPORT_JSON = PROJECT_DIR / "evaluation_report.json"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_test_suite() -> dict:
    loader = unittest.TestLoader()
    suite = loader.discover(str(PROJECT_DIR / "tests"), pattern="test_*.py")
    buffer = io.StringIO()
    runner = unittest.TextTestRunner(stream=buffer, verbosity=2)
    result = runner.run(suite)
    return {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "passed": result.wasSuccessful(),
        "output": buffer.getvalue(),
    }


def run_reproducibility_check() -> dict:
    import tempfile

    hashes = []
    timings = []
    for run in range(3):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "output.csv"
            start = time.perf_counter()
            run_pipeline(PROJECT_DIR, out_path)
            timings.append(round(time.perf_counter() - start, 4))
            hashes.append(sha256_of(out_path))
    return {
        "runs": 3,
        "sha256_hashes": hashes,
        "identical": len(set(hashes)) == 1,
        "execution_time_seconds_per_run": timings,
    }


def validate_output_schema() -> dict:
    output_path = PROJECT_DIR / "output.csv"
    df = pd.read_csv(output_path, dtype=str)
    issues = []
    if list(df.columns) != OUTPUT_COLUMNS:
        issues.append(f"column mismatch: {list(df.columns)}")
    if df["flagged"].isna().any():
        issues.append("missing values in flagged")
    if df["route"].isna().any() or df["week_of"].isna().any() or df["cost_per_tonne_km"].isna().any():
        issues.append("missing values in key fields")
    valid_verdicts = {"Yes", "No", "No (justified)"}
    if not set(df["flagged"]).issubset(valid_verdicts):
        issues.append(f"unexpected verdicts: {set(df['flagged']) - valid_verdicts}")
    unexplained = df[df["flagged"] == "Yes"]
    if unexplained["matched_note_id"].notna().any():
        issues.append("unexplained rows must have blank matched_note_id")
    justified = df[df["flagged"] == "No (justified)"]
    if justified["matched_note_id"].isna().any():
        issues.append("justified rows must cite a note")
    weeks = pd.to_datetime(df["week_of"], errors="coerce")
    if weeks.isna().any():
        issues.append("unparseable week_of values")
    elif (weeks.dt.weekday != 0).any():
        issues.append("week_of values that are not Mondays")
    if not df["cost_per_tonne_km"].str.match(r"^\d+\.\d{2}$").all():
        issues.append("cost_per_tonne_km not in two-decimal format")
    return {
        "valid": not issues,
        "issues": issues,
        "rows": len(df),
        "columns": list(df.columns),
        "missing_values_per_column": {c: int(df[c].isna().sum()) for c in df.columns},
    }


def main() -> None:
    print("Running test suite...")
    test_results = run_test_suite()
    print(f"tests: {test_results['tests_run']} run, "
          f"{test_results['failures']} failures, {test_results['errors']} errors")

    print("Running reproducibility check (3 runs)...")
    reproducibility = run_reproducibility_check()
    print(f"reproducibility identical: {reproducibility['identical']}")

    print("Validating output.csv schema...")
    schema_check = validate_output_schema()
    print(f"schema valid: {schema_check['valid']}")

    metrics = run_pipeline(PROJECT_DIR, PROJECT_DIR / "output.csv")
    solution.RUN_METRICS_JSON.write_text(
        json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
    )

    report = {
        "all_passed": test_results["passed"] and reproducibility["identical"] and schema_check["valid"],
        "test_results": test_results,
        "reproducibility": reproducibility,
        "output_schema_check": schema_check,
        "summary_counts": {
            "shipment_records_processed": metrics["shipment_records_processed"],
            "route_week_output_rows": metrics["route_week_output_rows"],
            "flagged_unexplained_rows": metrics["flagged_unexplained_rows"],
            "justified_rows": metrics["justified_rows"],
            "unflagged_rows": metrics["unflagged_rows"],
        },
        "llm_usage": {
            "llm_calls": 0,
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            "estimated_model_cost_usd": 0.0,
        },
    }
    EVALUATION_REPORT_JSON.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"evaluation_report.json written (all_passed={report['all_passed']})")


if __name__ == "__main__":
    main()
