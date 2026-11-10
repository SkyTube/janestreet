"""Behavioral contracts for the exchange and example strategies."""

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lob_sim.py"
sys.dont_write_bytecode = True


def simulator():
    assert SCRIPT.is_file(), "The simulator has not been implemented"
    spec = importlib.util.spec_from_file_location("lob_sim", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def example(name):
    return ROOT / "examples" / (name + ".py")


def check_accounting(test, report):
    test.assertEqual(sum(report["decomposition"].values()), report["pnl"]["fair"])
    test.assertEqual(report["cash"] + report["position"] * report["final_fair_ticks"]
                     * report["cash_per_tick"], report["pnl"]["fair"])
    for key in ("spread_captured", "adverse_selection_1s"):
        test.assertEqual(sum(c[key] for c in report["counterparties"].values()),
                         report["decomposition"][key])
    test.assertEqual(report["maker_fills"] + report["taker_fills"], report["fills"])
    test.assertEqual(report["maker_lots"] + report["taker_lots"], report["filled_lots"])
    for counterparty in report["counterparties"].values():
        test.assertNotIn("pnl_fair", counterparty)
        for key in ("fills", "lots", "spread_captured", "adverse_selection_1s"):
            test.assertEqual(counterparty["maker"][key] + counterparty["taker"][key],
                             counterparty[key])
        for horizon, diagnostic in counterparty["markouts"].items():
            for key in ("total", "observed_fills", "observed_lots", "truncated_fills",
                        "truncated_lots"):
                test.assertEqual(sum(counterparty[r]["markouts"][horizon][key]
                                     for r in ("maker", "taker")), diagnostic[key])


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.sim = simulator()
        self.engine = self.sim.Engine(position_limit=10)

    def test_price_time_and_partial_fills_at_resting_price(self):
        e = self.engine
        first = e.place("background", "sell", 102, 2)
        second = e.place("noise", "sell", 102, 3)
        better = e.place("informed", "sell", 101, 1)
        e.place("learner", "buy", 105, 4)
        self.assertEqual([(f["price"], f["qty"], f["counterparty"])
                          for f in e.entrants["learner"].fills],
                         [(101, 1, "informed"), (102, 2, "background"),
                          (102, 1, "noise")])
        self.assertNotIn(first, e.orders)
        self.assertNotIn(better, e.orders)
        self.assertEqual(e.orders[second].qty, 2)
        self.assertEqual(e.entrants["learner"].position, 4)
        self.assertIsInstance(e.entrants["learner"].cash, int)

    def test_self_trade_cancels_aggressor_without_skipping_priority(self):
        e = self.engine
        own = e.place("learner", "sell", 101, 2)
        other = e.place("background", "sell", 102, 3)
        e.place("learner", "buy", 103, 2)
        self.assertEqual(e.entrants["learner"].fills, [])
        self.assertEqual(e.orders[own].qty, 2)
        self.assertEqual(e.orders[other].qty, 3)
        self.assertEqual(e.entrants["learner"].position, 0)

    def test_unfilled_taker_never_enters_resting_book_or_midpoint_history(self):
        e = self.engine
        e.place("background", "buy", 99, 1)
        e.place("background", "sell", 103, 1)
        e.cancel_all("background")
        own = e.place("learner", "sell", 300, 1)
        e.take("noise", "buy", 1, 200)
        e.remember_mid()
        self.assertEqual(e.entrants["learner"].fills, [])
        self.assertEqual(list(e.orders), [own])
        self.assertEqual(e.orders[own].qty, 1)
        self.assertEqual(e.mid(), 10100)
        self.assertEqual(e.mark_at(0), 10100)

    def test_each_fill_callback_identifies_its_execution_with_current_state(self):
        calls = []

        def observe(view):
            calls.append((view["last_fill"]["price"], view["last_fill"]["qty"],
                          view["position"], view["cash"], view["book"]["asks"]))
            view["last_fill"]["price"] = -1
            return []

        e = self.sim.Engine(strategy=observe)
        e.running = True
        e.place("background", "sell", 101, 1)
        e.place("background", "sell", 102, 2)
        e.place("learner", "buy", 103, 3)
        e.advance(0)
        self.assertEqual(calls, [(101, 1, 3, -30506, []),
                                 (102, 2, 3, -30506, [])])
        self.assertEqual(e.view("tick")["last_fill"]["price"], 102)

    def test_position_limit_reserves_all_open_same_side_orders(self):
        e = self.engine
        e.place("learner", "buy", 90, 6)
        self.assertIsNone(e.place("learner", "buy", 89, 5))
        e.place("learner", "sell", 110, 10)
        self.assertEqual(e.entrants["learner"].rejected_orders, 1)
        self.assertEqual(len(e.orders), 2)
        e.place("noise", "sell", 90, 6)
        self.assertEqual(e.entrants["learner"].position, 6)
        self.assertIsNone(e.place("learner", "buy", 89, 5))

    def test_actions_arrive_after_latency_and_use_current_risk(self):
        e = self.engine
        e.entrants["learner"].latency_us = 200
        e.submit([{"action": "place", "side": "buy", "price": 99, "qty": 7}])
        e.advance(199)
        self.assertEqual(e.orders, {})
        e.advance(200)
        self.assertEqual(len(e.orders), 1)
        self.assertEqual(e.time_us, 200)
        e.submit([{"action": "place", "side": "buy", "price": 98, "qty": 7}])
        e.advance(400)
        self.assertEqual(e.entrants["learner"].rejected_orders, 1)

    def test_invalid_actions_and_foreign_cancels_are_rejected(self):
        e = self.engine
        oid = e.place("background", "buy", 90, 3)
        e.submit([{"action": "cancel", "order_id": oid},
                  {"action": "place", "side": "buy", "price": True, "qty": 1},
                  {"action": "place", "side": "buy", "price": 90, "qty": 1.5},
                  {"action": "unknown"}])
        e.advance(e.entrants["learner"].latency_us)
        self.assertEqual(e.entrants["learner"].rejected_orders, 4)
        self.assertIn(oid, e.orders)

    def test_view_is_detached_and_hides_fair_and_participants(self):
        e = self.engine
        oid = e.place("learner", "buy", 99, 2)
        e.place("background", "sell", 101, 3)
        view = e.view("tick")
        view["own_orders"][0]["qty"] = 999
        view["book"]["bids"][0][1] = 999
        self.assertEqual(e.orders[oid].qty, 2)
        self.assertEqual(e.view("tick")["book"]["bids"], [[99, 2]])
        self.assertNotIn("fair", json.dumps(e.view("tick")))
        self.assertNotIn("background", json.dumps(e.view("tick")))

    def test_signed_markouts_and_exact_decomposition(self):
        e = self.engine
        e.place("background", "buy", 99, 10)
        e.place("background", "sell", 103, 10)
        e.place("learner", "buy", 98, 1)
        e.place("noise", "sell", 98, 11)
        e.advance(1_000)
        report = e.report()
        self.assertEqual(report["pnl"]["fair"],
                         e.entrants["learner"].cash + e.entrants["learner"].position * e.fair * self.sim.CASH_PER_TICK)
        self.assertEqual(report["pnl"]["mid"],
                         e.entrants["learner"].cash + e.entrants["learner"].position * report["final_mid_cash_units"])
        self.assertEqual(sum(report["decomposition"].values()), report["pnl"]["fair"])
        check_accounting(self, report)
        self.assertEqual(report["markouts"]["1s"]["truncated_fills"], 1)


class SimulationTests(unittest.TestCase):
    def run_sim(self, scenario, seed=1, latency=200, strategy="fixed_spread"):
        sim = simulator()
        return sim.run(scenario, example(strategy), seed=seed,
                       latency_us=latency, duration_s=10, position_limit=20)

    def test_json_is_byte_deterministic_and_seed_matters(self):
        simulator()

        args = [sys.executable, "-B", str(SCRIPT), "run", "--scenario", "calm",
                "--strategy", str(example("fixed_spread")), "--duration-s", "1",
                "--json", "--seed"]
        first = subprocess.check_output(args + ["7"])
        self.assertEqual(first, subprocess.check_output(args + ["7"]))
        self.assertNotEqual(first, subprocess.check_output(args + ["8"]))
        self.assertIsInstance(json.loads(first)["cash"], int)

    def test_strategy_runs_at_decisions_and_fills(self):
        sim = simulator()
        calls = []

        def quote(view):
            calls.append((view["time_us"], view["event"]))
            return [{"action": "cancel_all"},
                    {"action": "place", "side": "buy", "price": 9999, "qty": 1},
                    {"action": "place", "side": "sell", "price": 10001, "qty": 1}]

        e = sim.Engine(strategy=quote, duration_us=100_000)
        e.start()
        e.advance(100_000)
        self.assertIn((0, "tick"), calls)
        self.assertIn((e.decision_interval_us, "tick"), calls)
        self.assertTrue(any(event == "fill" for _, event in calls))

    def test_exercise_two_calm_spread_and_pnl_positive_each_seed(self):
        for seed in range(1, 6):
            with self.subTest(seed=seed):
                report = self.run_sim("calm", seed)
                self.assertGreater(report["pnl"]["fair"], 0)
                self.assertGreater(report["decomposition"]["spread_captured"], 0)

    def test_exercise_three_toxic_informed_and_noise_markouts_each_seed(self):
        for seed in range(1, 6):
            with self.subTest(seed=seed):
                report = self.run_sim("toxic", seed)
                self.assertLess(report["pnl"]["fair"], 0)
                # Counterparty types are takers here: the strategy is the maker.
                for owner, sign in (("informed", -1), ("noise", 1)):
                    value = report["counterparties"][owner]["maker"]["markouts"]["100ms"]
                    self.assertGreater(sign * value["per_lot"], 0, (owner, value))

    def test_jumpy_latency_informed_maker_lots_and_total_markout_each_seed(self):
        for seed in range(1, 6):
            with self.subTest(seed=seed):
                fast = self.run_sim("jumpy", seed, 50)["counterparties"]["informed"]["maker"]
                slow = self.run_sim("jumpy", seed, 2000)["counterparties"]["informed"]["maker"]
                self.assertGreater(slow["lots"], fast["lots"])
                self.assertLess(slow["markouts"]["1ms"]["total"],
                                fast["markouts"]["1ms"]["total"])

    def test_inventory_skew_reduces_trending_max_position(self):
        fixed = [self.run_sim("trending", seed)["max_abs_position"]
                 for seed in range(1, 6)]
        skew = [self.run_sim("trending", seed, strategy="inventory_skew")["max_abs_position"]
                for seed in range(1, 6)]
        for seed, (a, b) in enumerate(zip(skew, fixed), 1):
            self.assertLess(a, b, (seed, fixed, skew))

    def test_exercise_eight_jumpy_skew_reconciles_each_seed(self):
        for seed in range(1, 6):
            with self.subTest(seed=seed):
                report = self.run_sim("jumpy", seed, strategy="inventory_skew")
                check_accounting(self, report)
                for diagnostic in report["markouts"].values():
                    self.assertEqual(diagnostic["truncated_lots"], 0)


class ReportTests(unittest.TestCase):
    def test_markouts_exclude_censored_horizons_and_keep_exact_accounting(self):
        sim = simulator()
        e = sim.Engine()
        e.place("background", "buy", 99, 10)
        e.place("background", "sell", 103, 10)
        e.place("learner", "buy", 100, 1)
        e.place("noise", "sell", 100, 1)
        e.advance(1_000)
        e.cancel_all("background")
        e.place("background", "buy", 101, 10)
        e.place("background", "sell", 105, 10)
        e.advance(1_500)
        e.place("learner", "buy", 102, 1)
        e.place("noise", "sell", 102, 1)
        e.advance(2_000)
        report = e.report()
        one_ms = report["markouts"]["1ms"]
        self.assertEqual(one_ms.get("observed_lots"), 1)
        self.assertEqual(one_ms["total"], 300)
        self.assertEqual(one_ms["per_lot"], 300)
        self.assertEqual(one_ms["truncated_fills"], 1)
        self.assertIsNone(report["markouts"]["1s"]["per_lot"])
        self.assertEqual(sum(report["decomposition"].values()), report["pnl"]["fair"])

    def test_scenarios_prints_one_description_per_line(self):
        sim = simulator()
        lines = subprocess.check_output([sys.executable, "-B", str(SCRIPT), "scenarios"],
                                        text=True).splitlines()
        self.assertEqual(len(lines), 5)
        for name, line in zip(sorted(sim.SCENARIOS), lines):
            self.assertTrue(line.startswith(name + ": "), line)

    def test_observation_tail_preserves_terminal_metrics_and_observes_last_fill(self):
        sim = simulator()
        e = sim.Engine(duration_us=2_000)
        e.start()
        e.advance(1_999)
        e.place("learner", "buy", 9999, 1)
        e.place("noise", "sell", 9999, 1)
        e.submit([{"action": "place", "side": "buy", "price": 10010, "qty": 1}])
        e.advance(2_000)
        before = e.report()
        self.assertTrue(hasattr(e, "finish"), "The terminal observation tail is missing")
        result = e.finish()["learner"]
        for key in ("pnl", "cash", "position", "duration_us", "final_fair_ticks",
                    "final_mid_cash_units", "quote_uptime", "avg_abs_position",
                    "max_abs_position", "fills", "accepted_lots"):
            self.assertEqual(result[key], before[key], key)
        self.assertEqual(e.time_us, 1_002_000)
        self.assertEqual(result["markouts"]["1s"]["truncated_fills"], 0)
        self.assertEqual(result["markouts"]["1s"]["observed_lots"], 1)
        self.assertEqual(result["markouts"]["1s"]["total"],
                         e.mark_at(1_001_999) - 9999 * sim.CASH_PER_TICK)
        self.assertEqual(sum(result["decomposition"].values()), result["pnl"]["fair"])
        check_accounting(self, result)
        self.assertFalse(any(o.owner == "learner" for o in e.orders.values()))

    def test_observation_tail_marks_withdrawal_before_next_background_event(self):
        sim = simulator()
        e = sim.Engine(duration_us=1_000)
        e.start()
        e.place("learner", "buy", 9999, 1)
        terminal = e.finish()["learner"]
        self.assertEqual(terminal["final_mid_cash_units"], 1_000_150)
        self.assertEqual(e.mark_at(1_001), 1_000_000)


class TournamentTests(unittest.TestCase):
    def test_tournament_strategies_trade_each_other_and_cash_conserves(self):
        sim = simulator()
        with tempfile.TemporaryDirectory() as directory:
            specs = []
            for name, side in (("maker", "sell"), ("taker", "buy")):
                path = pathlib.Path(directory) / (name + ".py")
                path.write_text("def on_tick(view):\n"
                    "    if view['time_us'] != 0 or view['event'] != 'tick': return []\n"
                    "    return [{'action':'place','side':'" + side + "','price':10000,'qty':1}]\n")
                specs.append(str(path))
            result = sim.tournament("calm", specs, seeds=[1], duration_s="0.001")
            reports = result["results"][0]["entrants"]
            for name in ("maker", "taker"):
                report = reports[name]
                self.assertEqual(report["counterparties"]["entrant"][name]["lots"], 1)
                check_accounting(self, report)
            self.assertEqual(sum(r["position"] for r in reports.values()), 0)
            self.assertEqual(sum(r["cash"] for r in reports.values()), -1)
            self.assertEqual(sum(r["pnl"]["fair"] for r in reports.values()), -1)

    def test_shared_book_maker_taker_risk_and_no_self_trade(self):
        sim = simulator()
        e = sim.Engine(entrants={"a": sim.Trader(None, 0, 3),
                                 "b": sim.Trader(None, 0, 3)})
        e.place("a", "sell", 10001, 2)
        e.place("a", "buy", 10002, 1)
        self.assertEqual(e.entrants["a"].fills, [])
        e.place("b", "buy", 10002, 1)
        self.assertEqual(e.entrants["a"].position, -1)
        self.assertEqual(e.entrants["b"].position, 1)
        self.assertIsNone(e.place("b", "buy", 9999, 3))
        self.assertEqual(e.entrants["b"].rejected_orders, 1)
        for owner, role in (("a", "maker"), ("b", "taker")):
            report = e.report(owner)
            self.assertEqual(report[role + "_lots"], 1)
            self.assertEqual(report["counterparties"]["entrant"][role]["lots"], 1)
            check_accounting(self, report)
            view = e.view("tick", owner)
            self.assertEqual(set(view), {"time_us", "event", "book", "last_trade",
                                         "last_fill", "own_orders", "position", "cash"})

    def test_equal_time_decisions_rotate_and_namespaces_are_fresh(self):
        sim = simulator()
        calls = []
        def strategy(owner):
            def tick(view):
                calls.append((view["time_us"], owner))
                return []
            return tick
        e = sim.Engine(entrants={name: sim.Trader(strategy(name), 0, 20)
                                 for name in ("a", "b", "c")}, duration_us=1000)
        e.start()
        e.advance(1000)
        self.assertEqual(calls, [(0, "a"), (0, "b"), (0, "c"),
                                 (500, "b"), (500, "c"), (500, "a"),
                                 (1000, "c"), (1000, "a"), (1000, "b")])
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "once.py"
            path.write_text("called = False\ndef on_tick(view):\n"
                            "    global called\n"
                            "    if called: return []\n"
                            "    called = True\n"
                            "    return [{'action':'place','side':'buy','price':1,'qty':1}]\n")
            result = sim.tournament("calm", [str(path), str(path)], seeds=[1, 2],
                                    duration_s="0.001")
            for seed_result in result["results"]:
                for report in seed_result["entrants"].values():
                    self.assertEqual(report["accepted_lots"], 1)

    def test_tournament_latency_comparison_and_accounting_each_seed(self):
        sim = simulator()
        result = sim.tournament("jumpy", ["fast=" + str(example("fixed_spread")) + "@50",
                                          "slow=" + str(example("fixed_spread")) + "@2000"])
        for seed_result in result["results"]:
            with self.subTest(seed=seed_result["seed"]):
                reports = seed_result["entrants"]
                fast, slow = [reports[n]["counterparties"]["informed"]["maker"]
                              for n in ("fast", "slow")]
                self.assertGreater(slow["lots"], fast["lots"])
                self.assertLess(slow["markouts"]["1ms"]["total"],
                                fast["markouts"]["1ms"]["total"])
                for report in reports.values():
                    check_accounting(self, report)
        self.assertEqual([r["mean_pnl"] for r in result["ranking"]],
                         sorted((r["mean_pnl"] for r in result["ranking"]), reverse=True))

    def test_tournament_cli_determinism_defaults_and_validation(self):
        args = [sys.executable, "-B", str(SCRIPT), "tournament", "--scenario", "calm",
                "--strategy", str(example("fixed_spread")), "--strategy",
                str(example("fixed_spread")), "--duration-s", "0.01", "--json"]
        first = subprocess.check_output(args)
        self.assertEqual(first, subprocess.check_output(args))
        result = json.loads(first)
        self.assertEqual(result["seeds"], [1, 2, 3, 4, 5])
        self.assertEqual([e["name"] for e in result["entrants"]],
                         ["fixed_spread", "fixed_spread_2"])
        self.assertTrue(all(e["latency_us"] == 200 for e in result["entrants"]))
        sim = simulator()
        path = str(example("fixed_spread"))
        for specs in ([path], [path] * 9, [path + "@-1", path],
                      [path + "@1.5", path], ["=" + path, path],
                      ["a=" + path, "a=" + path], ["a=", path]):
            with self.subTest(specs=specs), self.assertRaises(ValueError):
                sim.tournament("calm", specs, duration_s="0.001")
        for seeds in ("", "5-1", "1,,2", "1,1", "1-2,2", "abc"):
            with self.subTest(seeds=seeds):
                failed = subprocess.run(args + ["--seeds", seeds], capture_output=True)
                self.assertEqual(failed.returncode, 2)
                self.assertNotIn(b"Traceback", failed.stderr)

    def test_display_names_do_not_become_synthetic_owners(self):
        sim = simulator()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "a strategy.v2.py"
            path.write_text("def on_tick(view):\n    return []\n")
            result = sim.tournament("calm", [str(path)] + [name + "=" + str(path)
                                    for name in ("background", "noise", "informed")],
                                    seeds=[1], duration_s="0.1")
            self.assertEqual([e["name"] for e in result["entrants"]],
                             ["a strategy.v2", "background", "noise", "informed"])
            for report in result["results"][0]["entrants"].values():
                self.assertEqual(report["fills"], 0)
                self.assertEqual(report["accepted_lots"], 0)
                self.assertEqual(report["cash"], 0)
                self.assertEqual(report["position"], 0)


if __name__ == "__main__":
    unittest.main()
