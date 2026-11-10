---
name: "market-making-sim"
description: >
  Socratic market-making practice: strategies, inventory, latency and P&L in a
  deterministic simulator. Triggers: 'teach me market making', 'practice quoting',
  'simulate my market-making strategy', 'drill adverse selection'. Not for
  one-off definitions, real venues, live trading or evidence of real profitability.
---

# Market-Making Simulator

Teach through predictions, learner-written strategies and runs. Never connect to
a broker or venue or imply investment advice or real profitability.

## Session start

Confirm ongoing practice if ambiguous; answer one-off definitions normally.
With socratic-tutor, use its diagnostic, 0-5 map, one-question rule, strict
assessment and spaced review. Otherwise diagnose with three questions, one at a
time. Predict, code, execute, inspect, explain; require two demonstrations.
Write or repair learner code only on "just tell me", for that question alone.

## Setup and execution

Work from this skill's directory. Use Python 3.9-3.13 and no extra packages:

```sh
python3 -B scripts/lob_sim.py scenarios
python3 -B -m unittest discover -s tests -v
```

Replace SEED with each of 1, 2, 3, 4, 5; tournaments run all five by default.
Save the strategy and predict direction and mechanism first. Inspect actual JSON,
never reported results. Require every seed to pass; reserve 6-10 for fresh checks.
Change one feature at a time. Strategy files run trusted Python without a sandbox.

## Strategy interface

Define `on_tick(view)` returning at most 100 action dictionaries in a list:

- Place: `{"action":"place","side":"buy","price":9998,"qty":1}`; or `sell`.
- Cancel: `{"action":"cancel","order_id":12}`.
- Withdraw own resting orders: `{"action":"cancel_all"}`.

Calls occur every 500us and after own fills; actions arrive `--latency-us` later
(default 200), in return order. Cancels cannot withdraw in-flight messages.
Prices/lots must be positive integers; unsafe actions and foreign cancels reject.

Crosses execute at resting prices with partial fills and price-time priority.
Self-trade prevention cancels the incoming remainder. Arrival-time risk reserves
all open same-side lots; opposing orders cannot offset that reservation.

The detached view fields are:

- `time_us`, `event` (`tick` or `fill`), `position`, and `cash`.
- `book.bids` / `book.asks`: up to five `[price, lots]` levels each.
- `own_orders`: dictionaries with `order_id`, `side`, `price`, `qty`.
- `last_trade`: `time_us`, `price`, `qty`; `last_fill` also has `side`. Both
  start null. Fill callbacks identify their execution with current state; ticks
  show the latest fill. Fair and counterparty identities are hidden.

## Report reference

Maker means your resting order got hit; taker means your arriving order crossed.
An informed maker fill is against an informed taker. Noise/informed agents only take.

Report fields include:

- `cash`, `position`, `final_fair_ticks`, `final_mid_cash_units`, `cash_per_tick`.
- `pnl.fair` / `pnl.mid`: cash plus terminal position times the respective mark.
- `fills`, `filled_lots`, `maker_fills`, `maker_lots`, `taker_fills`, `taker_lots`.
- `accepted_lots`, `fill_ratio`, `quote_uptime`, `max_abs_position`,
  `avg_abs_position`, `rejected_orders`, `fees`, `rebates`.
- `decomposition`: `spread_captured`, `adverse_selection_1s`,
  `residual_inventory`, negative `fees`, positive `rebates`; sums to fair P&L.
- `markouts`: `1ms`, `100ms`, `1s`, each with `total`, `per_lot`, observed and
  truncated fills/lots, plus the same diagnostics for `maker` and `taker`.
- `counterparties`: noise, informed, background, entrant. Each has fills/lots,
  maker/taker counts and subgroups, spread, 1s movement, and all three markouts.
- `maker` / `taker`: own-role totals, spread, 1s movement and markouts.
- Scenario, seed, timing, position limit and synthetic `parameters`.

One tick per lot is 100 integer cash units. Spread measures execution against
pre-fill midpoint; 1s movement is signed future midpoint minus pre-fill midpoint.
Residual inventory attributes the rest to the terminal fair mark. Counterparty
spread/movement sum to those decomposition terms; no terminal P&L is attributed.

A markout measures signed future midpoint minus execution price, including spread.
`total` is cash units across observed lots; `per_lot` divides by those lots (null
with none). Negative is adverse. Do not add horizons. Total informed-maker losses
measure exposure to stale hits; per-lot averages hide the number of hits.

At stop, freeze P&L and metrics, withdraw entrant orders and ignore their messages.
Observe another second for complete markouts; raw reports expose truncation.
Missing book sides carry the last two-sided midpoint, initially 10000 ticks.
`fill_ratio` is filled/accepted lots including canceled quotes; `quote_uptime`
is the two-sided resting fraction; `avg_abs_position` is time weighted.

## Tournament

Use `tournament --scenario NAME --strategy SPEC --strategy SPEC` for 2-8 entrants.
A spec is `[NAME=]PATH[@LATENCY_US]` with default latency 200us. Names are nonempty
printable text, defaulting to filename stems with `_2`, `_3`, etc. for repeats.

Options: `--seeds 1-5` (also comma-separated integers/ranges), `--duration-s 10`,
`--position-limit 20`, `--json`. Entrants have separate cash, risk and fills;
They share one book, never self-trade, and get fresh namespaces per entrant/seed.

Every periodic tick calls entrants in command order, rotating the first entrant
by one each tick. Other equal-time events follow insertion order; zero-delay
actions join after that tick's decisions. Horizon marks use the final book state
at their timestamp. Rotation does not promise equal outcomes.

The table ranks mean fair P&L (ties by name), with worst P&L/seed, pooled
maker/taker lots, pooled 100ms markout per lot and maximum absolute position.
JSON `results` holds each seed's full report per entrant.

## Exercises

### 1. Book and price-time priority

Write a partial-fill trace and `book_probe.py` with passive one-lot replacement
quotes. Predict queue priority:

```sh
python3 -B scripts/lob_sim.py run --scenario calm \
  --strategy book_probe.py --seed SEED --json
```

Pass: fills in every seed, `filled_lots == fills`, zero rejections, and
`fill_ratio == filled_lots / accepted_lots`. The hand trace must match the
price-time/resting-price tests; aggregate P&L cannot prove FIFO.

### 2. Spread and edge

Predict why four ticks of quoted width need not guarantee profit:

```sh
python3 -B scripts/lob_sim.py run --scenario calm \
  --strategy examples/fixed_spread.py --seed SEED --json
```

Pass: positive `pnl.fair` and `decomposition.spread_captured` in every seed.
Explain quoted spread versus realized spread and the other P&L terms.

### 3. Adverse selection and markouts

Predict which counterparty selects stale quotes:

```sh
python3 -B scripts/lob_sim.py run --scenario toxic \
  --strategy examples/fixed_spread.py --seed SEED --json
```

Pass in every seed: negative fair P&L, negative 100ms `per_lot` markout against
informed takers, positive against noise takers. Read
`counterparties[TYPE].maker.markouts`; explain spread versus markout signs.

### 4. Inventory and skew

Predict how shifting quotes against position affects inventory:

```sh
python3 -B scripts/lob_sim.py run --scenario trending \
  --strategy examples/inventory_skew.py --seed SEED --json
```

Repeat with `examples/fixed_spread.py`. Pass: skew has lower `max_abs_position`
in every seed. Lower inventory need not improve profit.

### 5. Latency and stale quotes

Predict whether a delayed cancellation protects an already resting quote:

```sh
python3 -B scripts/lob_sim.py tournament --scenario jumpy \
  --strategy fast=examples/fixed_spread.py@50 \
  --strategy slow=examples/fixed_spread.py@2000 --seeds 1-5 --json
```

Pass in every seed: slow has more `counterparties.informed.maker.lots` and lower
`counterparties.informed.maker.markouts["1ms"].total` than fast. Also repeat as
separate `run` commands with `--latency-us 50` and `--latency-us 2000`; require
both inequalities there too. With 500us decisions after a 100us background
requote, fast can cancel at 550us before the 800us jump response; slow cannot.
Earlier scheduled informed checks can still arrive before that response.
These inequalities are calibrated for two entrants; a third changes the midpoint
that markouts are measured against, so compare lots and P&L there instead.

### 6. Fees, rebates and tick size

Write `fee_probe.py`: cross one lot each way on ticks when both sides exist,
do nothing on fills. Predict cost in ticks and units:

```sh
python3 -B scripts/lob_sim.py run --scenario thin --latency-us 0 \
  --strategy fee_probe.py --seed SEED --json
```

Pass: positive fees, zero rebates, gross decomposition minus fees equals fair
P&L. Convert fees to ticks; explain fractional-price rejection and costly crosses.

### 7. Risk limits

Write `risk_probe.py` requesting five lots, then repair sizing to reserve
pending/open same-side risk. Predict both runs:

```sh
python3 -B scripts/lob_sim.py run --scenario calm --position-limit 4 \
  --strategy risk_probe.py --seed SEED --json
```

Pass: initial runs reject with no fills; repaired runs fill with zero rejections
and `max_abs_position <= 4` in every seed. Repeat with longer arrival latency.

### 8. P&L decomposition

Predict spread, post-fill movement and residual inventory signs separately:

```sh
python3 -B scripts/lob_sim.py run --scenario jumpy \
  --strategy examples/inventory_skew.py --seed SEED --json
```

Pass in every seed: sum decomposition to fair P&L and reconcile
`cash + position * final_fair_ticks * cash_per_tick`. Sum counterparty spread and
1s movement to the corresponding decomposition terms exactly. Explain why adding
three markout horizons is wrong. Repeat on reserved seeds.

The model has one instrument, synthetic random walks/jumps and flow, delayed depth
and simplified fees; it omits hidden orders and cross-venue effects. Finish by
explaining a limitation and predicting an unseen scenario.
