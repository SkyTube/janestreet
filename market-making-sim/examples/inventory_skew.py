"""Shift against inventory and widen briefly after a fill."""


def on_tick(view):
    bids, asks = view["book"]["bids"], view["book"]["asks"]
    if not bids or not asks:
        return []
    center = (bids[0][0] + asks[0][0]) // 2 - view["position"]
    hit = view["last_fill"]
    width = 3 if hit and view["time_us"] - hit["time_us"] < 10_000 else 2
    return [
        {"action": "cancel_all"},
        {"action": "place", "side": "buy", "price": max(1, center - width), "qty": 1},
        {"action": "place", "side": "sell", "price": max(1, center + width), "qty": 1},
    ]
