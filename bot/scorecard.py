"""Signal scorecard: every signal sent is logged and scored 1 and 7 days later
against doing nothing (Grok's suggestion, 2026-10-09).

  * a SELL is good if the price fell afterwards (you avoided the drop),
  * a BUY is good if the price rose afterwards.
Both are charged 0.1% fee. If, after 30 scored signals, the average isn't
better than doing nothing, the rule doesn't deserve more money.
"""
from datetime import datetime, timedelta

FEE = 0.001


def record(log: list[dict], before: dict, after: dict, prices: dict[str, float], as_of: str, now: datetime,
           portion: str) -> None:
    """Adds one row per coin whose announced position changed."""
    for coin in set(before) | set(after):
        old, new = before.get(coin, 0.0), after.get(coin, 0.0)
        if abs(new - old) < 1e-9 or coin not in prices:
            continue
        log.append({"time": now.isoformat(timespec="minutes"), "day": as_of, "coin": coin, "portion": portion,
                    "action": "buy" if new > old else "sell", "from": round(old, 4), "to": round(new, 4),
                    "price": prices[coin]})


def score(log: list[dict], closes: dict[str, dict[str, float]]) -> None:
    """Fills in the 1-day and 7-day results once those daily closes exist."""
    for row in log:
        for days in (1, 7):
            key = f"edge_{days}d"
            if key in row:
                continue
            later = (datetime.strptime(row["day"], "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
            p = closes.get(row["coin"], {}).get(later)
            if p:
                move = p / row["price"] - 1
                row[key] = round(((move if row["action"] == "buy" else -move) - FEE) * 100, 2)


def summary(log: list[dict]) -> dict:
    done = [r for r in log if "edge_7d" in r]
    out = {"signals": len(log), "scored": len(done)}
    if done:
        out["avg_edge_7d"] = round(sum(r["edge_7d"] for r in done) / len(done), 2)
        out["hit_rate"] = round(100 * sum(r["edge_7d"] > 0 for r in done) / len(done))
        out["verdict"] = ("not enough signals yet" if len(done) < 30 else
                          "beating doing nothing" if out["avg_edge_7d"] > 0 else "not beating doing nothing")
    return out
