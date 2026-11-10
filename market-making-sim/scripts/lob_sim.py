#!/usr/bin/env python3
"""Deterministic exchange exercises; Python 3.9-3.13, standard library only.

Trusted strategy files define on_tick(view) -> a list of at most 100 actions:
  {"action": "place", "side": "buy", "price": 9998, "qty": 1}
  {"action": "cancel", "order_id": 12} or {"action": "cancel_all"}
Prices/lots are positive integers; use "sell" for the other side. Decisions
occur every 500us and after own fills. Actions arrive after the entrant's latency,
in return order. Cancellation cannot withdraw messages already in flight.

The detached view contains time_us, event (tick/fill), book (up to five bids/asks
[price, lots] levels), own_orders, last_trade, last_fill, position and cash.
Own orders contain order_id/side/price/qty; trades contain time_us/price/qty and
own fills also contain side. Missing trades/fills are None. Fill callbacks identify
the triggering execution but show current state. Fair and identities stay hidden.

All orders share price-time priority and execute at resting prices. Self-trade
prevention cancels the incoming remainder. Arrival-time risk reserves all open
same-side lots. Maker means the entrant's resting order was hit; taker means its
arriving order crossed. Equal-time events use insertion order. Each periodic tick
calls entrants in CLI order rotated by tick number; resulting actions and fill
callbacks join that same queue. Horizon marks use the final state at their time.

One tick per lot is 100 integer cash units. Markouts are signed future midpoint
minus execution price, in total cash units and per observed lot. They include
spread and must not be added across horizons. Counterparty maker/taker labels
always describe the entrant's role. Exact accounting identities are beside report
computation. Missing book sides carry the last two-sided midpoint (initially 10000).
Trading metrics freeze at stop; withdraw entrant orders, ignore their messages and
observe background trading for one second to complete all horizons. Raw report()
excludes unobserved markouts and counts truncation; complete runs have none.

Use scenarios, run --strategy PATH, or tournament --strategy [NAME=]PATH[@US]
--strategy [NAME=]PATH[@US]. Strategy code is trusted, not sandboxed. This synthetic
model omits hidden orders, other venues and real fee tiers; it proves no real edge.
"""

import argparse
import bisect
import contextlib
import heapq
import json
import math
import pathlib
import random
import sys
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation


CASH_PER_TICK = 100
HORIZONS = (("1ms", 1_000), ("100ms", 100_000), ("1s", 1_000_000))


@dataclass(frozen=True)
class Scenario:
    description: str
    noise_rate: int = 120
    informed_rate: int = 20
    fair_interval_us: int = 50_000
    step_ticks: int = 1
    up_probability: float = 0.5
    jump_probability: float = 0.01
    jump_ticks: int = 5
    lp_latency_us: int = 100
    informed_latency_us: int = 800
    half_spread: int = 4
    depth: int = 6
    threshold: int = 1
    maker_rebate: int = 1
    taker_fee: int = 2


SCENARIOS = {
    "calm": Scenario("Slow fair moves, plentiful balanced noise."),
    "trending": Scenario("Persistent upward drift challenges inventory control.",
                         fair_interval_us=10_000, up_probability=0.72),
    "toxic": Scenario("Fast fair moves and frequent informed taking.",
                      noise_rate=100, informed_rate=500, fair_interval_us=2_000,
                      step_ticks=3, jump_probability=0.05, jump_ticks=12,
                      lp_latency_us=2_000, informed_latency_us=50),
    # Decisions every 500us: fast 50us quotes can react to the background's 100us
    # requote before the jump response at 800us; slow 2000us cancels cannot.
    # Four-tick jumps expose new depth without routinely crossing stale quotes.
    # Frequent informed checks also catch replacements still arriving stale.
    # Previously scheduled checks may see the new fair before its 800us response.
    "jumpy": Scenario("Discrete jumps expose delayed cancellations.",
                      noise_rate=80, informed_rate=5_000, fair_interval_us=10_000,
                      jump_probability=0.2, jump_ticks=4),
    "thin": Scenario("Shallow, wide depth and sparse noise.", noise_rate=25,
                     half_spread=7, depth=1, lp_latency_us=2_000),
}


@dataclass
class Order:
    order_id: int
    owner: str
    side: str
    price: int
    qty: int


@dataclass
class Trader:
    strategy: object = None
    latency_us: int = 200
    position_limit: int = 20
    cash: int = 0
    position: int = 0
    fills: list = field(default_factory=list)
    last_fill: object = None
    accepted_qty: int = 0
    rejected_orders: int = 0
    fees: int = 0
    rebates: int = 0
    max_abs_position: int = 0
    position_time: int = 0
    quote_time: int = 0


def positive_int(value):
    return type(value) is int and value > 0


class Engine:
    """One shared book, independent accounts, one stable event queue."""

    def __init__(self, scenario="calm", strategy=None, seed=1, latency_us=200,
                 duration_us=10_000_000, position_limit=20, entrants=None):
        self.params = SCENARIOS[scenario]
        self.scenario = scenario
        self.entrants = (entrants if entrants is not None else
                         {"learner": Trader(strategy, latency_us, position_limit)})
        self.seed = seed
        self.rng = random.Random(seed)
        self.duration_us = duration_us
        self.decision_interval_us = 500
        self.time_us = 0
        self.fair = 10_000
        self.orders = {}
        self.last_trade = None
        self.events = []
        self.sequence = 0
        self.next_order_id = 1
        self.running = False
        self.observing = False
        self.last_mid = self.fair * CASH_PER_TICK
        self.mid_times = [0]
        self.mid_values = [self.last_mid]
        self.processed_events = 0

    def schedule(self, time_us, kind, data=None):
        self.sequence += 1
        heapq.heappush(self.events, (time_us, self.sequence, kind, data))

    def cancel_all(self, owner):
        for oid in [o.order_id for o in self.orders.values() if o.owner == owner]:
            del self.orders[oid]

    def levels(self, side):
        quantities = {}
        for order in self.orders.values():
            if order.side == side:
                quantities[order.price] = quantities.get(order.price, 0) + order.qty
        return [[p, quantities[p]] for p in sorted(quantities, reverse=side == "buy")]

    def mid(self):
        bids, asks = self.levels("buy"), self.levels("sell")
        if bids and asks:
            self.last_mid = (bids[0][0] + asks[0][0]) * (CASH_PER_TICK // 2)
        return self.last_mid

    def remember_mid(self):
        value = self.mid()
        if self.mid_times[-1] == self.time_us:
            self.mid_values[-1] = value
        elif value != self.mid_values[-1]:
            self.mid_times.append(self.time_us)
            self.mid_values.append(value)

    def place(self, owner, side, price, qty, rest=True):
        account = self.entrants.get(owner)
        if side not in ("buy", "sell") or not positive_int(price) or not positive_int(qty):
            if account is not None:
                account.rejected_orders += 1
            return None
        sign = 1 if side == "buy" else -1
        if account is not None:
            reserved = sum(o.qty for o in self.orders.values()
                           if o.owner == owner and o.side == side)
            if sign * account.position + reserved + qty > account.position_limit:
                account.rejected_orders += 1
                return None
            account.accepted_qty += qty
        oid = self.next_order_id
        self.next_order_id += 1
        incoming = Order(oid, owner, side, price, qty)
        opposing = sorted((o for o in self.orders.values() if o.side != side),
                          key=lambda o: (sign * o.price, o.order_id))
        for resting in opposing:
            if not incoming.qty or sign * (price - resting.price) < 0:
                break
            # Cancel the aggressor; skipping an own order would break priority.
            if resting.owner == owner:
                incoming.qty = 0
                break
            amount = min(incoming.qty, resting.qty)
            before_mid = self.mid()
            incoming.qty -= amount
            resting.qty -= amount
            if not resting.qty:
                del self.orders[resting.order_id]
            self.last_trade = {"time_us": self.time_us, "price": resting.price,
                               "qty": amount}
            for trader, trade_side, other, maker in (
                    (owner, side, resting.owner, False),
                    (resting.owner, resting.side, owner, True)):
                if trader in self.entrants:
                    self.record_fill(trader, trade_side, resting.price, amount,
                                     before_mid, other, maker)
        if incoming.qty and rest:
            self.orders[oid] = incoming
        self.remember_mid()
        return oid

    def record_fill(self, owner, side, price, qty, mid, counterparty, maker):
        account = self.entrants[owner]
        sign = 1 if side == "buy" else -1
        fee = 0 if maker else self.params.taker_fee * qty
        rebate = self.params.maker_rebate * qty if maker else 0
        account.cash += -sign * price * qty * CASH_PER_TICK - fee + rebate
        account.position += sign * qty
        account.fees += fee
        account.rebates += rebate
        account.max_abs_position = max(account.max_abs_position, abs(account.position))
        account.last_fill = {"time_us": self.time_us, "side": side, "price": price,
                             "qty": qty}
        account.fills.append(dict(account.last_fill, mid_before=mid, maker=maker,
                                  counterparty=("entrant" if counterparty in self.entrants
                                                else counterparty), fee=fee, rebate=rebate))
        if self.running and account.strategy is not None:
            self.schedule(self.time_us, "decision", (owner, dict(account.last_fill)))

    def view(self, event, owner="learner"):
        account = self.entrants[owner]
        own = [{"order_id": o.order_id, "side": o.side, "price": o.price, "qty": o.qty}
               for o in sorted(self.orders.values(), key=lambda o: o.order_id)
               if o.owner == owner]
        return {"time_us": self.time_us, "event": event,
                "book": {"bids": self.levels("buy")[:5], "asks": self.levels("sell")[:5]},
                "last_trade": dict(self.last_trade) if self.last_trade else None,
                "last_fill": dict(account.last_fill) if account.last_fill else None,
                "own_orders": own, "position": account.position, "cash": account.cash}

    def submit(self, actions, owner="learner"):
        account = self.entrants[owner]
        if not isinstance(actions, list) or len(actions) > 100:
            account.rejected_orders += 1
            return
        for action in actions:
            self.schedule(self.time_us + account.latency_us, "action",
                          (owner, dict(action) if isinstance(action, dict) else None))

    def apply(self, action, owner="learner"):
        account = self.entrants[owner]
        if not isinstance(action, dict):
            account.rejected_orders += 1
            return
        kind = action.get("action")
        if kind == "place" and set(action) == {"action", "side", "price", "qty"}:
            self.place(owner, action["side"], action["price"], action["qty"])
        elif kind == "cancel_all" and set(action) == {"action"}:
            self.cancel_all(owner)
        elif (kind == "cancel" and set(action) == {"action", "order_id"}
              and positive_int(action["order_id"])):
            order = self.orders.get(action["order_id"])
            if order is not None and order.owner != owner:
                account.rejected_orders += 1
            elif order is not None:
                del self.orders[order.order_id]
        else:
            account.rejected_orders += 1

    def poisson(self, kind, rate):
        delay = max(1, math.ceil(self.rng.expovariate(rate) * 1_000_000))
        self.schedule(self.time_us + delay, kind)

    def reprice(self, fair):
        self.cancel_all("background")
        for level in range(3):
            distance = self.params.half_spread + level * 2
            self.place("background", "buy", max(1, fair - distance), self.params.depth)
            self.place("background", "sell", fair + distance, self.params.depth)

    def take(self, owner, side, qty, price):
        self.place(owner, side, price, qty, rest=False)

    def informed(self):
        for side, sign in (("buy", 1), ("sell", -1)):
            limit = self.fair - sign * (self.params.threshold + 1)
            quantity = sum(o.qty for o in self.orders.values()
                           if o.owner != "informed" and o.side != side
                           and sign * (limit - o.price) >= 0)
            if quantity:
                self.take("informed", side, quantity, limit)

    def start(self):
        self.running = True
        self.reprice(self.fair)
        self.schedule(0, "tick")
        self.schedule(self.params.fair_interval_us, "fair")
        self.poisson("noise", self.params.noise_rate)
        self.poisson("informed", self.params.informed_rate)

    def decide(self, owner, fill=None):
        account = self.entrants[owner]
        if account.strategy is not None:
            view = self.view("fill" if fill else "tick", owner)
            if fill:
                view["last_fill"] = dict(fill)
            self.submit(account.strategy(view), owner)

    def dispatch(self, kind, data):
        p = self.params
        if self.observing and kind in ("action", "decision", "tick"):
            return
        if kind == "action":
            owner, action = data
            self.apply(action, owner)
        elif kind == "decision":
            self.decide(*data)
        elif kind == "tick":
            owners = list(self.entrants)
            rotation = (self.time_us // self.decision_interval_us) % len(owners)
            for owner in owners[rotation:] + owners[:rotation]:
                self.decide(owner)
            self.schedule(self.time_us + self.decision_interval_us, "tick")
        elif kind == "fair":
            direction = 1 if self.rng.random() < p.up_probability else -1
            jump = self.rng.random() < p.jump_probability
            self.fair = max(100, self.fair + direction * (p.jump_ticks if jump else p.step_ticks))
            self.schedule(self.time_us + p.lp_latency_us, "reprice", self.fair)
            self.schedule(self.time_us + p.informed_latency_us, "react")
            self.schedule(self.time_us + p.fair_interval_us, "fair")
        elif kind == "reprice":
            self.reprice(data)
        elif kind == "noise":
            side = "buy" if self.rng.random() < 0.5 else "sell"
            self.take("noise", side, self.rng.randint(1, 3),
                      self.fair + 100 if side == "buy" else max(1, self.fair - 100))
            self.poisson("noise", p.noise_rate)
        elif kind == "informed":
            self.schedule(self.time_us + p.informed_latency_us, "react")
            self.poisson("informed", p.informed_rate)
        elif kind == "react":
            self.informed()

    def elapse(self, time_us):
        elapsed = time_us - self.time_us
        for owner, account in self.entrants.items():
            account.position_time += elapsed * abs(account.position)
            sides = {o.side for o in self.orders.values() if o.owner == owner}
            if len(sides) == 2:
                account.quote_time += elapsed
        self.time_us = time_us

    def advance(self, until_us):
        if until_us < self.time_us:
            raise ValueError("time cannot move backwards")
        while self.events and self.events[0][0] <= until_us:
            time_us, _, kind, data = heapq.heappop(self.events)
            self.elapse(time_us)
            self.processed_events += 1
            if self.processed_events > 2_000_000:
                raise ValueError("event budget exceeded; check strategy feedback or duration")
            self.dispatch(kind, data)
            self.remember_mid()
        self.elapse(until_us)
        self.remember_mid()

    def mark_at(self, time_us):
        index = bisect.bisect_right(self.mid_times, min(time_us, self.time_us)) - 1
        return self.mid_values[index]

    def finish(self):
        """Freeze all accounts at the same stop, then complete their diagnostics."""
        self.advance(self.duration_us)
        terminal = {owner: self.report(owner) for owner in self.entrants}
        self.running = False
        self.observing = True
        for owner in self.entrants:
            self.cancel_all(owner)
        self.remember_mid()
        self.advance(self.duration_us + 1_000_000)
        for owner, result in terminal.items():
            observed = self.report(owner)
            for key in ("markouts", "counterparties", "maker", "taker"):
                result[key] = observed[key]
            result["decomposition"]["adverse_selection_1s"] = (
                observed["decomposition"]["adverse_selection_1s"])
            result["decomposition"]["residual_inventory"] = (
                result["pnl"]["fair"] - sum(value for key, value
                    in result["decomposition"].items() if key != "residual_inventory"))
        return terminal

    def fill_summary(self, fills):
        def role_summary(role_fills):
            result = {"fills": len(role_fills), "lots": sum(f["qty"] for f in role_fills),
                      "spread_captured": 0, "adverse_selection_1s": 0, "markouts": {}}
            for name, horizon in HORIZONS:
                result["markouts"][name] = {"total": 0, "per_lot": None,
                    "observed_fills": 0, "observed_lots": 0,
                    "truncated_fills": 0, "truncated_lots": 0}
            for fill in role_fills:
                signed_qty = fill["qty"] * (1 if fill["side"] == "buy" else -1)
                price = fill["price"] * CASH_PER_TICK
                # Counterparty spreads partition sum(s*q*(m-p)); movements
                # partition sum(s*q*(h-m)), exactly the decomposition terms.
                result["spread_captured"] += signed_qty * (fill["mid_before"] - price)
                result["adverse_selection_1s"] += signed_qty * (
                    self.mark_at(fill["time_us"] + 1_000_000) - fill["mid_before"])
                for name, horizon in HORIZONS:
                    diagnostic = result["markouts"][name]
                    observed = fill["time_us"] + horizon <= self.time_us
                    prefix = "observed" if observed else "truncated"
                    diagnostic[prefix + "_fills"] += 1
                    diagnostic[prefix + "_lots"] += fill["qty"]
                    if observed:
                        diagnostic["total"] += signed_qty * (
                            self.mark_at(fill["time_us"] + horizon) - price)
            for diagnostic in result["markouts"].values():
                if diagnostic["observed_lots"]:
                    diagnostic["per_lot"] = diagnostic["total"] / diagnostic["observed_lots"]
            return result

        result = role_summary(fills)
        for role in ("maker", "taker"):
            result[role] = role_summary([f for f in fills if f["maker"] == (role == "maker")])
            for count in ("fills", "lots"):
                result[role + "_" + count] = result[role][count]
            for name, diagnostic in result["markouts"].items():
                diagnostic[role] = dict(result[role]["markouts"][name])
        return result

    def report(self, owner="learner"):
        account = self.entrants[owner]
        final_fair = self.fair * CASH_PER_TICK
        final_mid = self.mid()
        summary = self.fill_summary(account.fills)
        counterparties = {kind: self.fill_summary([f for f in account.fills
                                                   if f["counterparty"] == kind])
                          for kind in ("noise", "informed", "background", "entrant")}
        # s=side sign, q=lots, p=execution, m=pre-fill mid, h=1s mid, F=final fair:
        # cash + position*F = sum(s*q*(m-p)) + sum(s*q*(h-m))
        #                   + sum(s*q*(F-h)) - fees + rebates, all integer cash units.
        residual = sum((1 if f["side"] == "buy" else -1) * f["qty"]
                       * (final_fair - self.mark_at(f["time_us"] + 1_000_000))
                       for f in account.fills)
        return {
            "scenario": self.scenario, "seed": self.seed, "duration_us": self.time_us,
            "latency_us": account.latency_us, "decision_interval_us": self.decision_interval_us,
            "position_limit": account.position_limit, "cash_per_tick": CASH_PER_TICK,
            "parameters": asdict(self.params), "cash": account.cash,
            "position": account.position, "final_fair_ticks": self.fair,
            "final_mid_cash_units": final_mid,
            "pnl": {"fair": account.cash + account.position * final_fair,
                    "mid": account.cash + account.position * final_mid},
            "decomposition": {"spread_captured": summary["spread_captured"],
                "adverse_selection_1s": summary["adverse_selection_1s"],
                "residual_inventory": residual, "fees": -account.fees, "rebates": account.rebates},
            "fees": account.fees, "rebates": account.rebates, "markouts": summary["markouts"],
            "counterparties": counterparties, "maker": summary["maker"], "taker": summary["taker"],
            "maker_fills": summary["maker_fills"], "maker_lots": summary["maker_lots"],
            "taker_fills": summary["taker_fills"], "taker_lots": summary["taker_lots"],
            "fills": summary["fills"], "filled_lots": summary["lots"],
            "accepted_lots": account.accepted_qty,
            "fill_ratio": summary["lots"] / account.accepted_qty if account.accepted_qty else 0,
            "quote_uptime": account.quote_time / self.time_us if self.time_us else 0,
            "max_abs_position": account.max_abs_position,
            "avg_abs_position": account.position_time / self.time_us if self.time_us else 0,
            "rejected_orders": account.rejected_orders,
        }


def duration_us(value):
    try:
        micros = Decimal(str(value)) * 1_000_000
        if not micros.is_finite() or micros <= 0 or micros != micros.to_integral_value():
            raise ValueError
        return int(micros)
    except (InvalidOperation, ValueError):
        raise ValueError("duration must be positive with at most six decimal places")


def load_strategy(path):
    path = pathlib.Path(path)
    namespace = {"__name__": "strategy", "__file__": str(path)}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    strategy = namespace.get("on_tick")
    if not callable(strategy):
        raise ValueError("strategy must define on_tick(view)")
    return strategy


def validate_run(scenario, position_limit):
    if scenario not in SCENARIOS:
        raise ValueError("unknown scenario")
    if not positive_int(position_limit):
        raise ValueError("position limit must be a positive integer")


def run(scenario, strategy_path, seed=1, latency_us=200, duration_s=10, position_limit=20):
    validate_run(scenario, position_limit)
    if type(latency_us) is not int or latency_us < 0:
        raise ValueError("latency must be a nonnegative integer")
    with contextlib.redirect_stdout(sys.stderr):
        engine = Engine(scenario, load_strategy(strategy_path), seed, latency_us,
                        duration_us(duration_s), position_limit)
        engine.start()
        return engine.finish()["learner"]


def parse_seeds(value):
    seeds = []
    try:
        for part in value.split(","):
            bounds = [int(n) for n in part.split("-")]
            if len(bounds) == 1:
                seeds.append(bounds[0])
            elif len(bounds) == 2 and 0 <= bounds[0] <= bounds[1] < bounds[0] + 100:
                seeds.extend(range(bounds[0], bounds[1] + 1))
            else:
                raise ValueError
        if not seeds or len(seeds) > 100 or min(seeds) < 0 or len(set(seeds)) != len(seeds):
            raise ValueError
    except ValueError:
        raise ValueError("seeds must be distinct nonnegative integers/ranges, at most 100")
    return seeds


def parse_entrants(specs):
    if not 2 <= len(specs) <= 8:
        raise ValueError("tournament requires 2-8 strategies")
    entrants = []
    names = set()
    for spec in specs:
        explicit, separator, path = str(spec).partition("=")
        if not separator:
            path = explicit
        path, latency_separator, latency = path.rpartition("@") if "@" in path else (path, "", "")
        if latency_separator and (not latency.isascii() or not latency.isdecimal()):
            raise ValueError("strategy latency must be a nonnegative integer")
        if not path:
            raise ValueError("strategy path cannot be empty")
        name = explicit if separator else pathlib.Path(path).stem
        if not name.strip() or not name.isprintable():
            raise ValueError("entrant names must be nonempty printable text")
        if separator and name in names:
            raise ValueError("entrant names must be unique")
        base, suffix = name, 2
        while name in names:
            name, suffix = base + "_" + str(suffix), suffix + 1
        names.add(name)
        entrants.append({"name": name, "path": path,
                         "latency_us": int(latency) if latency_separator else 200})
    return entrants


def tournament(scenario, strategy_specs, seeds=range(1, 6), duration_s=10, position_limit=20):
    validate_run(scenario, position_limit)
    entrants = parse_entrants(strategy_specs)
    seeds = parse_seeds(seeds) if isinstance(seeds, str) else list(seeds)
    if (not seeds or len(seeds) > 100 or any(type(s) is not int or s < 0 for s in seeds)
            or len(set(seeds)) != len(seeds)):
        raise ValueError("seeds must be 1-100 distinct nonnegative integers")
    stop = duration_us(duration_s)
    results = []
    with contextlib.redirect_stdout(sys.stderr):
        for seed in seeds:
            # Display names must never collide with synthetic order owners.
            accounts = {"entrant:" + str(i): Trader(load_strategy(e["path"]),
                        e["latency_us"], position_limit) for i, e in enumerate(entrants)}
            engine = Engine(scenario, seed=seed, duration_us=stop, entrants=accounts)
            engine.start()
            reports = engine.finish()
            results.append({"seed": seed, "entrants": {
                e["name"]: reports[owner] for e, owner in zip(entrants, accounts)}})
    ranking = []
    for entrant in entrants:
        name = entrant["name"]
        reports = [r["entrants"][name] for r in results]
        worst = min(reports, key=lambda r: r["pnl"]["fair"])
        lots = sum(r["markouts"]["100ms"]["observed_lots"] for r in reports)
        ranking.append({"name": name,
            "mean_pnl": sum(r["pnl"]["fair"] for r in reports) / len(reports),
            "worst_seed": worst["seed"], "worst_pnl": worst["pnl"]["fair"],
            "maker_lots": sum(r["maker_lots"] for r in reports),
            "taker_lots": sum(r["taker_lots"] for r in reports),
            "markout_100ms_per_lot": (sum(r["markouts"]["100ms"]["total"] for r in reports)
                                       / lots if lots else None),
            "max_abs_position": max(r["max_abs_position"] for r in reports)})
    ranking.sort(key=lambda r: (-r["mean_pnl"], r["name"]))
    return {"scenario": scenario, "seeds": seeds, "entrants": entrants,
            "ranking": ranking, "results": results}


def text_report(report):
    print("Scenario: {scenario}; seed: {seed}; duration: {duration_us} us".format(**report))
    print("P&L (cash units): fair {fair}; mid {mid}".format(**report["pnl"]))
    print("Cash: {cash}; position: {position}; final fair: {final_fair_ticks} ticks".format(**report))
    print("Decomposition (sums to fair P&L):")
    for name, value in report["decomposition"].items():
        print("  {}: {}".format(name, value))
    print("Fills: {fills}; lots: {filled_lots}; maker {maker_fills}/{maker_lots}; "
          "taker {taker_fills}/{taker_lots} (fills/lots)".format(**report))
    def marks(group):
        return "; ".join("{} {} ({})".format(
            name, d["total"], "n/a" if d["per_lot"] is None else "{:.1f}".format(d["per_lot"]))
            for name, d in group["markouts"].items())

    print("Markouts, total (per lot), negative is adverse:")
    print("  all: " + marks(report))
    print("By counterparty and your role (lots; spread; 1s movement; markouts):")
    for owner, values in report["counterparties"].items():
        for role in ("maker", "taker"):
            group = values[role]
            if group["lots"]:
                print("  {} as {}: {} lots; spread {}; movement {}; {}".format(
                    owner, role, group["lots"], group["spread_captured"],
                    group["adverse_selection_1s"], marks(group)))
    print("Fill ratio: {fill_ratio:.4f}; quote uptime: {quote_uptime:.2%}; "
          "max |position|: {max_abs_position}; average: {avg_abs_position:.2f}".format(**report))
    print("Rejected actions: {rejected_orders}; fees: {fees}; rebates: {rebates}".format(**report))


def tournament_text(report):
    print("Tournament: {}; seeds {}".format(report["scenario"], report["seeds"]))
    print("Mean/worst P&L in cash units; maker/taker lots pooled; 100ms per observed lot.")
    width = max(7, max(len(r["name"]) for r in report["ranking"]))
    print(" # {:<{}} {:>12} {:>12} {:>6} {:>8} {:>8} {:>12} {:>8}".format(
        "Entrant", width, "Mean P&L", "Worst P&L", "Seed", "Maker", "Taker", "100ms/lot", "Peak |q|"))
    for place, entrant in enumerate(report["ranking"], 1):
        markout = entrant["markout_100ms_per_lot"]
        print("{:>2} {:<{}} {:>12.2f} {:>12} {:>6} {:>8} {:>8} {:>12} {:>8}".format(
            place, entrant["name"], width, entrant["mean_pnl"], entrant["worst_pnl"],
            entrant["worst_seed"], entrant["maker_lots"], entrant["taker_lots"],
            "n/a" if markout is None else "{:.2f}".format(markout), entrant["max_abs_position"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("scenarios", help="list the named scenarios")
    for name in ("run", "tournament"):
        command = subparsers.add_parser(name, help="run trusted local strategies")
        command.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
        command.add_argument("--strategy", action="append" if name == "tournament" else "store",
                             required=True, metavar="[NAME=]PATH[@US]" if name == "tournament" else "PATH")
        command.add_argument("--duration-s", default="10")
        command.add_argument("--position-limit", type=int, default=20)
        command.add_argument("--json", action="store_true")
        if name == "run":
            command.add_argument("--seed", type=int, default=1)
            command.add_argument("--latency-us", type=int, default=200)
        else:
            command.add_argument("--seeds", default="1-5")
    args = parser.parse_args()
    if args.command == "scenarios":
        for name in sorted(SCENARIOS):
            print("{}: {}".format(name, SCENARIOS[name].description))
        return
    try:
        if args.command == "run":
            report = run(args.scenario, args.strategy, args.seed, args.latency_us,
                         args.duration_s, args.position_limit)
        else:
            report = tournament(args.scenario, args.strategy, args.seeds,
                                args.duration_s, args.position_limit)
    except (OSError, ValueError, SyntaxError, TypeError) as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
    elif args.command == "run":
        text_report(report)
    else:
        tournament_text(report)


if __name__ == "__main__":
    main()
