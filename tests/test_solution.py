from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import solution
from solution import (
    ContextNote,
    OWN_HISTORY_THRESHOLD_PCT,
    PEER_THRESHOLD_PCT,
    add_baselines,
    aggregate_weekly,
    build_output_rows,
    classify_note,
    detect_anomalies,
    load_notes,
    monday_of_week,
    note_can_justify,
    retrieve_note,
    run_pipeline,
)

PROJECT_DIR = Path(__file__).resolve().parents[1]
NOTES_PATH = PROJECT_DIR / "context_notes.csv"


def make_weekly(rows):
    return pd.DataFrame(rows)


class TestMondayWeekAssignment(unittest.TestCase):
    def test_monday_maps_to_itself(self):
        self.assertEqual(monday_of_week(pd.Timestamp("2024-01-01")), pd.Timestamp("2024-01-01"))

    def test_sunday_maps_to_previous_monday(self):
        self.assertEqual(monday_of_week(pd.Timestamp("2024-01-07")), pd.Timestamp("2024-01-01"))

    def test_midweek_maps_to_monday(self):
        self.assertEqual(monday_of_week(pd.Timestamp("2024-01-03")), pd.Timestamp("2024-01-01"))
        self.assertEqual(monday_of_week(pd.Timestamp("2025-03-08")), pd.Timestamp("2025-03-03"))

    def test_sunday_after_flood_window(self):
        self.assertEqual(monday_of_week(pd.Timestamp("2025-03-09")), pd.Timestamp("2025-03-03"))
        self.assertEqual(monday_of_week(pd.Timestamp("2025-03-10")), pd.Timestamp("2025-03-10"))


class TestAggregation(unittest.TestCase):
    def test_cost_per_tonne_km_formula(self):
        shipments = pd.DataFrame(
            [
                {"shipment_id": "A", "route": "X-Y", "route_type": "Short",
                 "quantity_tonnes": 10.0, "distance_km": 100.0, "freight_cost_inr": 5000,
                 "shipment_date": pd.Timestamp("2024-01-01")},
                {"shipment_id": "B", "route": "X-Y", "route_type": "Short",
                 "quantity_tonnes": 20.0, "distance_km": 100.0, "freight_cost_inr": 9000,
                 "shipment_date": pd.Timestamp("2024-01-02")},
            ]
        )
        weekly, zero_denom = aggregate_weekly(shipments)
        self.assertEqual(zero_denom, 0)
        self.assertEqual(len(weekly), 1)
        row = weekly.iloc[0]
        self.assertEqual(row["route"], "X-Y")
        self.assertEqual(row["week_of"], pd.Timestamp("2024-01-01"))
        self.assertAlmostEqual(row["total_tonne_km"], 3000.0)
        self.assertAlmostEqual(row["cost_per_tonne_km"], 14000.0 / 3000.0)

    def test_zero_denominator_group_dropped(self):
        shipments = pd.DataFrame(
            [
                {"shipment_id": "A", "route": "X-Y", "route_type": "Short",
                 "quantity_tonnes": 0.0, "distance_km": 100.0, "freight_cost_inr": 5000,
                 "shipment_date": pd.Timestamp("2024-01-01")},
            ]
        )
        weekly, zero_denom = aggregate_weekly(shipments)
        self.assertEqual(zero_denom, 1)
        self.assertEqual(len(weekly), 0)

    def test_weeks_span_monday_to_sunday(self):
        shipments = pd.DataFrame(
            [
                {"shipment_id": "A", "route": "X-Y", "route_type": "Short",
                 "quantity_tonnes": 1.0, "distance_km": 10.0, "freight_cost_inr": 100,
                 "shipment_date": pd.Timestamp("2024-01-07")},
                {"shipment_id": "B", "route": "X-Y", "route_type": "Short",
                 "quantity_tonnes": 1.0, "distance_km": 10.0, "freight_cost_inr": 300,
                 "shipment_date": pd.Timestamp("2024-01-08")},
            ]
        )
        weekly, _ = aggregate_weekly(shipments)
        self.assertEqual(len(weekly), 2)
        self.assertEqual(set(weekly["week_of"]), {pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-08")})


def synthetic_weekly():
    rows = []
    weeks = pd.date_range("2024-01-01", periods=12, freq="7D")
    for i, week in enumerate(weeks):
        rows.append({"route": "R1", "route_type": "Short", "week_of": week,
                     "cost_per_tonne_km": 2.0 + i * 0.1,
                     "total_cost": 100.0, "total_tonne_km": 50.0, "shipment_count": 1})
    for i, week in enumerate(weeks):
        rows.append({"route": "R2", "route_type": "Short", "week_of": week,
                     "cost_per_tonne_km": 2.0 + i * 0.1,
                     "total_cost": 100.0, "total_tonne_km": 50.0, "shipment_count": 1})
    return pd.DataFrame(rows)


class TestBaselines(unittest.TestCase):
    def test_own_history_uses_trailing_eight_weeks_strictly_before_current(self):
        weekly = add_baselines(synthetic_weekly())
        r1 = weekly[weekly["route"] == "R1"].reset_index(drop=True)
        tenth = r1.iloc[9]
        self.assertAlmostEqual(tenth["own_history_avg"], r1.iloc[1:9]["cost_per_tonne_km"].mean())
        first = r1.iloc[0]
        self.assertTrue(pd.isna(first["own_history_avg"]))

    def test_no_look_ahead(self):
        weekly = synthetic_weekly()
        last_week = weekly["week_of"].max()
        future = weekly.copy()
        future.loc[future["week_of"] == last_week, "cost_per_tonne_km"] = 999.0
        baseline_now = add_baselines(weekly)
        baseline_future = add_baselines(future)
        merged = baseline_now.merge(
            baseline_future[["route", "week_of", "own_history_avg"]],
            on=["route", "week_of"],
            suffixes=("_now", "_future"),
        )
        self.assertTrue(merged["own_history_avg_now"].equals(merged["own_history_avg_future"]))

    def test_fewer_than_eight_observations_uses_all_available(self):
        weekly = add_baselines(synthetic_weekly())
        r1 = weekly[weekly["route"] == "R1"].reset_index(drop=True)
        third = r1.iloc[2]
        self.assertAlmostEqual(third["own_history_avg"], r1.iloc[0:2]["cost_per_tonne_km"].mean())

    def test_peer_average_excludes_self(self):
        weekly = add_baselines(synthetic_weekly())
        r1 = weekly[weekly["route"] == "R1"].reset_index(drop=True)
        r2 = weekly[weekly["route"] == "R2"].reset_index(drop=True)
        self.assertTrue((r1["peer_avg"].to_numpy() == r2["cost_per_tonne_km"].to_numpy()).all())

    def test_missing_peer_group(self):
        weekly = synthetic_weekly()
        weekly = weekly[weekly["route"] != "R2"].reset_index(drop=True)
        weekly = add_baselines(weekly)
        self.assertTrue(weekly["peer_avg"].isna().all())
        self.assertTrue(weekly["vs_peer_pct"].isna().all())


class TestAnomalyThresholds(unittest.TestCase):
    def row(self, cost, own, peer):
        return pd.DataFrame(
            [{"route": "R1", "route_type": "Short", "week_of": pd.Timestamp("2024-03-04"),
              "cost_per_tonne_km": cost, "total_cost": 100.0, "total_tonne_km": 50.0,
              "shipment_count": 1, "own_history_avg": own, "peer_avg": peer,
              "vs_own_history_pct": (cost - own) / own * 100,
              "vs_peer_pct": (cost - peer) / peer * 100}]
        )

    def test_own_history_threshold_boundary(self):
        at_threshold = detect_anomalies(self.row(cost=11.0, own=10.0, peer=10.0))
        self.assertTrue(at_threshold.iloc[0]["is_flagged"])
        below = detect_anomalies(self.row(cost=10.99, own=10.0, peer=10.0))
        self.assertFalse(below.iloc[0]["is_flagged"])

    def test_peer_threshold_boundary(self):
        at_threshold = detect_anomalies(self.row(cost=11.5, own=11.0, peer=10.0))
        self.assertTrue(at_threshold.iloc[0]["is_flagged"])
        below = detect_anomalies(self.row(cost=11.49, own=11.0, peer=10.0))
        self.assertFalse(below.iloc[0]["is_flagged"])

    def test_peer_triggers_independently_of_own_history(self):
        row = detect_anomalies(self.row(cost=11.6, own=10.05, peer=10.0))
        self.assertTrue(row.iloc[0]["is_flagged"])

    def test_falling_cost_never_flagged(self):
        row = detect_anomalies(self.row(cost=8.0, own=10.0, peer=10.0))
        self.assertFalse(row.iloc[0]["is_flagged"])


class TestNoteClassification(unittest.TestCase):
    def setUp(self):
        self.notes = load_notes(NOTES_PATH)

    def test_only_cost_increase_notes_are_evidence(self):
        evidence_ids = {n.note_id for n in self.notes if n.is_evidence}
        self.assertEqual(evidence_ids, {"N001", "N002", "N003"})

    def test_non_evidence_notes_rejected(self):
        rejected = {n.note_id for n in self.notes if not n.is_evidence}
        self.assertEqual(rejected, {"N004", "N005", "N006", "N007", "N008", "N009", "N010"})

    def test_flood_note_window(self):
        n001 = next(n for n in self.notes if n.note_id == "N001")
        self.assertEqual(n001.window_start, pd.Timestamp("2025-02-24"))
        self.assertEqual(n001.window_end, pd.Timestamp("2025-03-08"))

    def test_festival_note_single_week_window(self):
        n002 = next(n for n in self.notes if n.note_id == "N002")
        self.assertEqual(n002.window_start, pd.Timestamp("2025-01-20"))
        self.assertEqual(n002.window_end, pd.Timestamp("2025-01-26"))

    def test_diesel_note_bounded_window(self):
        n003 = next(n for n in self.notes if n.note_id == "N003")
        self.assertEqual(n003.window_start, pd.Timestamp("2025-05-05"))
        self.assertEqual(n003.window_end, pd.Timestamp("2025-06-29"))

    def test_route_specific_matching(self):
        n001 = next(n for n in self.notes if n.note_id == "N001")
        self.assertEqual(n001.applies_to, "Chennai-Bangalore")


class TestNoteRetrieval(unittest.TestCase):
    def setUp(self):
        self.notes = load_notes(NOTES_PATH)

    def test_flood_note_matches_affected_weeks(self):
        self.assertIsNotNone(retrieve_note("Chennai-Bangalore", pd.Timestamp("2025-02-24"), self.notes))
        self.assertIsNotNone(retrieve_note("Chennai-Bangalore", pd.Timestamp("2025-03-03"), self.notes))

    def test_flood_note_does_not_match_after_window(self):
        self.assertIsNone(retrieve_note("Chennai-Bangalore", pd.Timestamp("2025-03-10"), self.notes))
        self.assertIsNone(retrieve_note("Chennai-Bangalore", pd.Timestamp("2025-03-17"), self.notes))

    def test_route_specific_note_not_applied_to_other_routes(self):
        self.assertIsNone(retrieve_note("Mumbai-Pune", pd.Timestamp("2025-02-24"), self.notes))
        self.assertIsNone(retrieve_note("Ahmedabad-Mumbai", pd.Timestamp("2025-03-03"), self.notes))

    def test_all_routes_note_applies_to_every_route(self):
        for route in ["Mumbai-Pune", "Delhi-Jaipur", "Chennai-Bangalore"]:
            self.assertIsNotNone(retrieve_note(route, pd.Timestamp("2025-05-12"), self.notes))

    def test_all_routes_note_not_applied_after_bounded_window(self):
        self.assertIsNone(retrieve_note("Mumbai-Pune", pd.Timestamp("2025-09-15"), self.notes))
        self.assertIsNone(retrieve_note("Delhi-Jaipur", pd.Timestamp("2025-06-30"), self.notes))

    def test_recovery_note_never_justifies(self):
        for week in pd.date_range("2025-03-17", periods=6, freq="7D"):
            self.assertIsNone(retrieve_note("Chennai-Bangalore", week, self.notes))

    def test_non_evidence_notes_never_justify(self):
        flagged_weeks = pd.date_range("2024-01-01", periods=104, freq="7D")
        for note_id in ["N004", "N005", "N006", "N007", "N008", "N009", "N010"]:
            note = next(n for n in self.notes if n.note_id == note_id)
            self.assertFalse(note.is_evidence)
        for week in flagged_weeks:
            matched = retrieve_note("Kolkata-Bhubaneswar", week, self.notes)
            if matched is not None:
                self.assertIn(matched.note_id, {"N001", "N002", "N003"})


class TestUniformNoteJustification(unittest.TestCase):
    def _note(self, note_id, applies_to, start, end):
        return ContextNote(
            note_id=note_id,
            date=pd.Timestamp(start),
            applies_to=applies_to,
            text="test note",
            is_evidence=True,
            window_start=pd.Timestamp(start),
            window_end=pd.Timestamp(end),
            summary="test note",
        )

    def _weekly_row(self, route, week, cost, own_hist, peer_cost):
        own = 10.0
        peer = 10.0
        return pd.DataFrame(
            [{"route": route, "route_type": "Short", "week_of": pd.Timestamp(week),
              "cost_per_tonne_km": cost, "total_cost": 100.0, "total_tonne_km": 10.0,
              "shipment_count": 1,
              "own_history_avg": own if own_hist is None else own_hist,
              "peer_avg": peer if peer_cost is None else peer_cost,
              "vs_own_history_pct": ((cost - (own if own_hist is None else own_hist)) / (own if own_hist is None else own_hist)) * 100,
              "vs_peer_pct": ((cost - (peer if peer_cost is None else peer_cost)) / (peer if peer_cost is None else peer_cost)) * 100,
              "is_flagged": True}]
        )

    def test_all_routes_note_cannot_justify_peer_triggered_only_flag(self):
        week = pd.Timestamp("2025-06-23")
        weekly = pd.DataFrame(
            [
                {"route": "R1", "route_type": "Short", "week_of": week,
                 "cost_per_tonne_km": 12.06, "total_cost": 100.0, "total_tonne_km": 10.0,
                 "shipment_count": 1, "own_history_avg": 11.35, "peer_avg": 9.5,
                 "vs_own_history_pct": 6.2, "vs_peer_pct": 20.6, "is_flagged": True},
            ]
        )
        notes = [self._note("N003", "All Routes", "2025-05-05", "2025-06-29")]
        rows = build_output_rows(weekly, notes)
        self.assertEqual(rows[0]["flagged"], "Yes")
        self.assertEqual(rows[0]["matched_note_id"], "")
        self.assertIn("rejected", rows[0]["reason"])

    def test_all_routes_note_can_justify_own_triggered_flag(self):
        week = pd.Timestamp("2025-05-12")
        weekly = pd.DataFrame(
            [
                {"route": "R1", "route_type": "Short", "week_of": week,
                 "cost_per_tonne_km": 11.16, "total_cost": 100.0, "total_tonne_km": 10.0,
                 "shipment_count": 1, "own_history_avg": 10.0, "peer_avg": 9.5,
                 "vs_own_history_pct": 11.6, "vs_peer_pct": 6.7, "is_flagged": True},
            ]
        )
        notes = [self._note("N003", "All Routes", "2025-05-05", "2025-06-29")]
        rows = build_output_rows(weekly, notes)
        self.assertEqual(rows[0]["flagged"], "No (justified)")
        self.assertEqual(rows[0]["matched_note_id"], "N003")

    def test_route_specific_note_can_justify_peer_triggered_flag(self):
        week = pd.Timestamp("2025-02-24")
        weekly = pd.DataFrame(
            [
                {"route": "R1", "route_type": "Short", "week_of": week,
                 "cost_per_tonne_km": 13.15, "total_cost": 100.0, "total_tonne_km": 10.0,
                 "shipment_count": 1, "own_history_avg": 10.0, "peer_avg": 9.5,
                 "vs_own_history_pct": 31.5, "vs_peer_pct": 38.7, "is_flagged": True},
            ]
        )
        notes = [self._note("N001", "R1", "2025-02-24", "2025-03-08")]
        rows = build_output_rows(weekly, notes)
        self.assertEqual(rows[0]["flagged"], "No (justified)")
        self.assertEqual(rows[0]["matched_note_id"], "N001")

    def test_note_can_justify_unit(self):
        all_routes = self._note("N003", "All Routes", "2025-05-05", "2025-06-29")
        route_specific = self._note("N001", "R1", "2025-02-24", "2025-03-08")
        self.assertFalse(note_can_justify(all_routes, 6.2, 20.6))
        self.assertTrue(note_can_justify(all_routes, 11.6, 6.7))
        self.assertTrue(note_can_justify(route_specific, 6.2, 20.6))
        self.assertFalse(note_can_justify(all_routes, float("nan"), 20.6))


class TestOutputContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.metrics = run_pipeline(PROJECT_DIR, Path(cls.temp.name) / "output.csv")
        cls.out = pd.read_csv(Path(cls.temp.name) / "output.csv", dtype=str)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_exact_columns_and_order(self):
        self.assertEqual(list(self.out.columns), solution.OUTPUT_COLUMNS)

    def test_row_count_matches_route_weeks(self):
        self.assertEqual(len(self.out), 728)
        self.assertEqual(self.out["route"].nunique(), 7)

    def test_week_of_is_monday(self):
        weekdays = pd.to_datetime(self.out["week_of"]).dt.weekday
        self.assertTrue((weekdays == 0).all())

    def test_week_of_format(self):
        self.assertTrue(self.out["week_of"].str.match(r"^\d{4}-\d{2}-\d{2}$").all())

    def test_matched_note_id_blank_when_unexplained(self):
        unexplained = self.out[self.out["flagged"] == "Yes"]
        self.assertTrue(unexplained["matched_note_id"].isna().all())

    def test_justified_rows_cite_exact_note(self):
        justified = self.out[self.out["flagged"] == "No (justified)"]
        self.assertEqual(set(justified["matched_note_id"]), {"N001", "N002", "N003"})
        for note_id, group in justified.groupby("matched_note_id"):
            self.assertTrue(group["reason"].str.contains(note_id, regex=False).all())

    def test_unflagged_rows_have_no_note(self):
        unflagged = self.out[self.out["flagged"] == "No"]
        self.assertTrue(unflagged["matched_note_id"].isna().all())

    def test_cost_format_two_decimals(self):
        self.assertTrue(self.out["cost_per_tonne_km"].str.match(r"^\d+\.\d{2}$").all())

    def test_no_missing_values_in_required_fields(self):
        required = ["route", "week_of", "cost_per_tonne_km", "flagged", "reason"]
        self.assertFalse(self.out[required].isna().any().any())

    def test_metrics_counts_sum_to_rows(self):
        m = self.metrics
        self.assertEqual(
            m["flagged_unexplained_rows"] + m["justified_rows"] + m["unflagged_rows"],
            m["route_week_output_rows"],
        )
        self.assertEqual(m["llm_calls"], 0)
        self.assertEqual(m["llm_input_tokens"], 0)
        self.assertEqual(m["llm_output_tokens"], 0)
        self.assertEqual(m["estimated_model_cost_usd"], 0.0)


class TestSampleContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        out_path = Path(cls.temp.name) / "output.csv"
        run_pipeline(PROJECT_DIR, out_path)
        cls.out = pd.read_csv(out_path, dtype=str)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def row(self, route, week):
        return self.out[(self.out["route"] == route) & (self.out["week_of"] == week)].iloc[0]

    def test_sample_row_delhi_jaipur(self):
        r = self.row("Delhi-Jaipur", "2024-11-11")
        self.assertEqual(r["cost_per_tonne_km"], "4.17")
        self.assertEqual(r["vs_own_history"], "+35.5% vs this route's past average")
        self.assertEqual(r["vs_similar_routes"], "+21.0% vs similar-length routes this week")
        self.assertEqual(r["flagged"], "Yes")
        self.assertTrue(pd.isna(r["matched_note_id"]))

    def test_sample_row_ahmedabad_mumbai(self):
        r = self.row("Ahmedabad-Mumbai", "2025-01-20")
        self.assertEqual(r["cost_per_tonne_km"], "3.29")
        self.assertEqual(r["vs_own_history"], "+29.5% vs this route's past average")
        self.assertEqual(r["vs_similar_routes"], "+22.5% vs similar-length routes this week")
        self.assertEqual(r["flagged"], "No (justified)")
        self.assertEqual(r["matched_note_id"], "N002")

    def test_sample_row_mumbai_pune(self):
        r = self.row("Mumbai-Pune", "2025-09-15")
        self.assertEqual(r["cost_per_tonne_km"], "3.98")
        self.assertEqual(r["vs_own_history"], "+9.2% vs this route's past average")
        self.assertEqual(r["vs_similar_routes"], "+23.6% vs similar-length routes this week")
        self.assertEqual(r["flagged"], "Yes")
        self.assertTrue(pd.isna(r["matched_note_id"]))


class TestDieselNotePeerJustification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        out_path = Path(cls.temp.name) / "output.csv"
        run_pipeline(PROJECT_DIR, out_path)
        cls.out = pd.read_csv(out_path, dtype=str)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def row(self, route, week):
        return self.out[(self.out["route"] == route) & (self.out["week_of"] == week)].iloc[0]

    def test_mumbai_pune_2025_06_23_peer_triggered_not_justified(self):
        r = self.row("Mumbai-Pune", "2025-06-23")
        self.assertEqual(r["flagged"], "Yes")
        self.assertTrue(pd.isna(r["matched_note_id"]))
        self.assertIn("rejected", r["reason"])
        self.assertIn("N003", r["reason"])

    def test_mumbai_delhi_2025_05_12_own_triggered_justified(self):
        r = self.row("Mumbai-Delhi", "2025-05-12")
        self.assertEqual(r["flagged"], "No (justified)")
        self.assertEqual(r["matched_note_id"], "N003")

    def test_peer_of_mumbai_pune_fell_same_week(self):
        r = self.row("Delhi-Jaipur", "2025-06-23")
        self.assertEqual(r["flagged"], "No")


if __name__ == "__main__":
    unittest.main()
