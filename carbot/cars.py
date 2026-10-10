"""Nigerian used-car price bot: collects listings, works out the usual price
for each model and year, and picks the listings priced furthest below it.

Sources (checked 2026-10-10 from GitHub's servers):
  * Autochek (autochek.africa): readable, structured data incl. mileage,
    inspection grade, accident flag. Main source.
  * Carlots / Carmart (carlots.ng, carmart.ng): readable HTML listings.
  * Jiji and Cars45 block GitHub's servers (403 / 405), so they're not used.
A "deal" is only as good as the comparison: a car listed 30% under similar
cars may have a reason (accident, flood, fake papers). Always inspect.
"""
import csv
import html
import json
import logging
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36", "Accept-Language": "en-NG,en;q=0.9"}
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "cars"
PAUSE = 1.5            # seconds between page requests, to be polite
MIN_COMPS = 5          # similar cars needed before calling something a deal
TWO_WORD = {"land cruiser", "range rover", "grand cherokee", "rav 4", "c class", "e class", "glk 350",
            "highlander hybrid", "santa fe", "grand vitara"}
MAKES = ["toyota", "lexus", "honda", "mercedes-benz", "mercedes", "benz", "bmw", "ford", "hyundai", "kia",
         "nissan", "acura", "volkswagen", "peugeot", "mazda", "mitsubishi", "land rover", "jeep", "audi",
         "infiniti", "chevrolet", "suzuki", "volvo", "porsche", "gac", "changan", "geely", "innoson",
         "jetour", "chery", "jac", "renault", "dodge", "cadillac", "subaru", "opel", "pontiac", "gmc",
         "rolls-royce", "bentley", "tesla", "mini", "isuzu", "jaguar"]


def _num(x) -> float | None:
    try:
        return float(re.sub(r"[^\d.]", "", str(x))) or None
    except ValueError:
        return None


def make_model(title: str) -> tuple[str, str]:
    t = re.sub(r"\b(19|20)\d{2}\b", " ", title.lower())
    t = re.sub(r"[^a-z0-9\- ]", " ", t)
    for mk in MAKES:
        m = re.search(rf"\b{re.escape(mk)}\b\s+([a-z0-9\-]+)(?:\s+([a-z0-9\-]+))?", t)
        if m:
            make = {"mercedes": "mercedes-benz", "benz": "mercedes-benz"}.get(mk, mk)
            model = m.group(1)
            if m.group(2) and f"{model} {m.group(2)}" in TWO_WORD:
                model = f"{model} {m.group(2)}"
            return make, model.replace("rav 4", "rav4")
    return "", ""


def year_of(title: str, year=None) -> int | None:
    if year:
        return int(year)
    m = re.search(r"\b(19[89]\d|20[0-3]\d)\b", title)
    return int(m.group(1)) if m else None


# ---------- sources ----------

def autochek(max_pages: int = 60) -> list[dict]:
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        r = requests.get(f"https://autochek.africa/ng/cars-for-sale?page_number={page}", headers=H, timeout=30)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        cars = (json.loads(m.group(1))["props"]["pageProps"].get("cars") or {}).get("result") if m else None
        if not cars or all(c["id"] in seen for c in cars):
            break
        for c in cars:
            if c["id"] in seen or c.get("sold"):
                continue
            seen.add(c["id"])
            make, model = make_model(c.get("title", ""))
            out.append({
                "source": "Autochek", "id": c["id"], "title": c.get("title", "").strip(),
                "make": make, "model": model, "year": year_of(c.get("title", ""), c.get("year")),
                "price": _num(c.get("marketplacePrice")), "mileage": _num(c.get("mileage")),
                "condition": c.get("sellingCondition") or "", "city": c.get("city") or c.get("state") or "",
                "transmission": c.get("transmission") or "", "fuel": c.get("fuelType") or "",
                "inspected": bool(c.get("inspected")), "grade": c.get("gradeScore"),
                "accident": bool(c.get("accidented")),
                "url": c.get("websiteUrl") or f"https://autochek.africa/ng/car/{c['id']}",
            })
        time.sleep(PAUSE)
    return out


def carlots(max_pages: int = 30) -> list[dict]:
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        url = "https://carlots.ng/" if page == 1 else f"https://carlots.ng/search?page={page}"
        r = requests.get(url, headers=H, timeout=30)
        items = re.findall(r'<a class="title" href="([^"]+)">(.*?)</a>.*?<div class="price"><span>([^<]+)</span>',
                           r.text, re.S)
        new = [(u, t, p) for u, t, p in items if u not in seen]
        if not new:
            break
        for u, t, p in new:
            seen.add(u)
            t = html.unescape(t).strip()
            make, model = make_model(t)
            out.append({"source": "Carlots", "id": u.rsplit("_", 1)[-1].split(".")[0], "title": t,
                        "make": make, "model": model, "year": year_of(t), "price": _num(p), "mileage": None,
                        "condition": "", "city": "", "transmission": "", "fuel": "", "inspected": False,
                        "grade": None, "accident": False, "url": u})
        time.sleep(PAUSE)
    return out


SOURCES = {"Autochek": autochek, "Carlots": carlots}


# ---------- pricing ----------

def _key(c: dict) -> str:
    return f"{c['make']} {c['model']}".strip()


def fair_values(cars: list[dict]) -> None:
    """Adds the usual price for similar cars (same make, model, year +-1,
    same foreign/local condition when known) and how far below it each car is."""
    groups: dict[str, list[dict]] = {}
    for c in cars:
        if c["make"] and c["year"] and c["price"] and c["price"] > 300_000:
            groups.setdefault(_key(c), []).append(c)
    for c in cars:
        c["fair"], c["comps"], c["below"] = None, 0, None
        pool = [o for o in groups.get(_key(c), []) if o is not c and abs(o["year"] - (c["year"] or 0)) <= 1
                and (not c["condition"] or not o["condition"] or o["condition"] == c["condition"])]
        if len(pool) < MIN_COMPS or not c["price"]:
            continue
        fair = statistics.median(o["price"] for o in pool)
        miles = [o["mileage"] for o in pool if o["mileage"]]
        if c["mileage"] and len(miles) >= MIN_COMPS:
            # about 1% of value per 10,000 km above or below the usual mileage, capped at +-15%
            adj = max(-0.15, min(0.15, (statistics.median(miles) - c["mileage"]) / 10_000 * 0.01))
            fair *= 1 + adj
        c["fair"], c["comps"] = round(fair, -3), len(pool)
        c["below"] = round((1 - c["price"] / fair) * 100, 1)


def deals(cars: list[dict], top: int = 15, budget: float | None = None) -> list[dict]:
    """Best value: priced under the usual price for similar cars, not accident-flagged.
    Anything more than 40% under is dropped as probably a fake or a part-payment price."""
    ok = [c for c in cars if c["below"] is not None and 5 <= c["below"] <= 40 and not c["accident"]
          and (not budget or c["price"] <= budget)]
    return sorted(ok, key=lambda c: (-(c["below"] + (5 if c["inspected"] else 0)), c["price"]))[:top]


def deal_text(ds: list[dict], total: int, health: dict) -> str:
    lines = [f"🚗 Car deals today: {len(ds)} best-value cars out of {total:,} listings", ""]
    for i, c in enumerate(ds, 1):
        extra = ", ".join(x for x in (
            f"{c['mileage']:,.0f} km" if c["mileage"] else "", c["condition"], c["city"],
            "inspected ✓" if c["inspected"] else "") if x)
        lines.append(f"{i}. {html.escape(c['title'])} ({c['year']}): ₦{c['price']:,.0f}, "
                     f"{c['below']:.0f}% under the usual ₦{c['fair']:,.0f} ({c['comps']} similar cars)"
                     + (f"\n   {html.escape(extra)}" if extra else "") + f"\n   {c['url']}")
    lines += ["", "Usual price = middle price of the same model and year (±1) on these sites. "
              "Cheap can mean hidden problems: inspect it, check papers (customs duty, VIN) "
              "and never pay before seeing the car.",
              "Sources: " + ", ".join(f"{k} {v}" for k, v in health.items())]
    return "\n".join(lines)


# ---------- run ----------

FIELDS = ["source", "id", "title", "make", "model", "year", "price", "fair", "below", "comps", "mileage",
          "condition", "city", "transmission", "fuel", "inspected", "grade", "accident", "url"]


def save(cars: list[dict], ds: list[dict], health: dict, now: datetime) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "listings.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(cars, key=lambda c: (c["make"], c["model"], c["year"] or 0, c["price"] or 0)))
    (OUT / "deals.json").write_text(json.dumps(
        {"as_of": now.isoformat(timespec="minutes"), "total": len(cars), "health": health, "deals": ds}, indent=1))


def main() -> None:
    import sys
    logging.basicConfig(level=logging.INFO)
    now = datetime.now(timezone.utc)
    cars, health = [], {}
    for name, fn in SOURCES.items():
        try:
            got = fn()
            cars += got
            health[name] = f"ok ({len(got)})"
        except Exception as e:  # one site failing shouldn't stop the others
            health[name] = f"failed ({type(e).__name__})"
            log.warning("%s failed: %s", name, e)
    budget = next((float(a.split("=")[1]) for a in sys.argv if a.startswith("--budget=")), None)
    fair_values(cars)
    ds = deals(cars, budget=budget)
    save(cars, ds, health, now)
    text = deal_text(ds, len(cars), health)
    print(text)
    if "--send" in sys.argv and ds:
        sys.path.insert(0, str(ROOT))
        from bot.alerts import send_telegram
        send_telegram(text)


if __name__ == "__main__":
    main()
