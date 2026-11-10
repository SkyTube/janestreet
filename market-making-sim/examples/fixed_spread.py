"""Quote one lot two ticks either side of the observed midpoint."""


def on_tick(view):
    bids, asks = view["book"]["bids"], view["book"]["asks"]
    if not bids or not asks:
        return []
    center = (bids[0][0] + asks[0][0]) // 2
    return [
        {"action": "cancel_all"},
        {"action": "place", "side": "buy", "price": max(1, center - 2), "qty": 1},
        {"action": "place", "side": "sell", "price": center + 2, "qty": 1},
    ]
