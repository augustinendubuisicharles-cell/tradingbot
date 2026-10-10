"""Nigerian used-car price bot: collects listings, works out the usual price
for each model and year, and picks the listings priced furthest below it.

Sources (checked 2026-10-10 from GitHub's servers):
  * Autochek (autochek.africa): readable, structured data incl. mileage,
    inspection grade, accident flag. Main source.
  * Cars.ng: readable HTML, 16 cars a page.
  * Carlots / Carmart (carlots.ng, carmart.ng): readable HTML, one page per brand.
  * Jiji, Cars45, Jumia and Nairaland block GitHub's servers, so they're not used.
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
MAX_SPREAD = 0.35      # skip models whose middle-half prices differ by more than this (mixed trims)
MAX_BELOW = 35         # further under than this is more likely a fake or a part-payment price
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

def autochek(max_pages: int = 60, base: str = "https://autochek.africa/ng/cars-for-sale") -> list[dict]:
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        r = requests.get(f"{base}?page_number={page}", headers=H, timeout=30)
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


def _carlots_page(url: str, seen: set) -> list[dict]:
    r = requests.get(url, headers=H, timeout=30)
    out = []
    for chunk in r.text.split('<a class="title" href="')[1:]:
        m = re.match(r'([^"]+)">(.*?)</a>.*?<div class="price"><span>([^<]+)</span>', chunk, re.S)
        if not m or m.group(1) in seen:
            continue
        u, t, p = m.group(1), html.unescape(m.group(2)).strip(), m.group(3)
        seen.add(u)
        loc = re.search(r'class="location[^"]*">.*?<span>([^<]+)</span>', chunk, re.S)
        desc = re.search(r'class="description[^"]*">([^<]*)<', chunk)
        km = re.search(r"mileage[^\d]{0,20}([\d,]+)\s*(k|thousand)?", (desc.group(1) if desc else ""), re.I)
        make, model = make_model(t)
        out.append({"source": "Carlots", "id": u.rsplit("_", 1)[-1].split(".")[0], "title": t,
                    "make": make, "model": model, "year": year_of(t), "price": _num(p),
                    "mileage": (_num(km.group(1)) or 0) * (1000 if km.group(2) else 1) or None if km else None,
                    "condition": "foreign" if re.search(r"tokunbo|foreign", t, re.I) else "",
                    "city": loc.group(1).strip() if loc else "", "transmission": "", "fuel": "",
                    "inspected": False, "grade": None, "accident": False, "url": u})
    return out


def carlots_region(word: str) -> list[dict]:
    """Carlots' own page for one state or city, if it has one."""
    r = requests.get("https://carlots.ng/", headers=H, timeout=30)
    links = sorted(set(re.findall(rf'href="(https://carlots\.ng/{word}[a-z\-]*-[rc]\d+)"', r.text)))
    out, seen = [], set()
    for u in links:
        time.sleep(PAUSE)
        out += _carlots_page(u, seen)
    return out


def carlots() -> list[dict]:
    """carlots.ng (also carmart.ng): the main list plus one page per brand (about 52 cars each)."""
    seen: set = set()
    r = requests.get("https://carlots.ng/cars", headers=H, timeout=30)
    brands = sorted(set(re.findall(r'href="(https://carlots\.ng/cars/[a-z\-]+)"', r.text)))
    out = _carlots_page("https://carlots.ng/cars", seen)
    for b in brands:
        time.sleep(PAUSE)
        try:
            out += _carlots_page(b, seen)
        except Exception as e:
            log.warning("carlots %s failed: %s", b, e)
    return out


def carsng(max_pages: int = 60, base: str = "https://cars.ng/for-sale") -> list[dict]:
    """cars.ng: 16 cars a page, title like 'Kia Sorento 2014 for Sale in Lagos'."""
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        r = requests.get(f"{base}?page={page}", headers=H, timeout=30)
        if r.status_code != 200:
            break
        new = 0
        for chunk in r.text.split('class="offer-name')[1:]:
            t = re.search(r'title="([^"]+)"', chunk)
            u = re.search(r'href="(https://cars\.ng/[^"]+/for-sale/[^"]+)"', chunk)
            p = re.search(r'fw-bold">\s*₦\s*([\d,]+)', chunk)
            if not (t and u and p) or u.group(1) in seen:
                continue
            seen.add(u.group(1))
            new += 1
            title = html.unescape(t.group(1)).replace(" for Sale", "").strip()
            props = [x.strip() for x in re.findall(r'<h4 class="properties">([^<]+)</h4>', chunk)]
            loc = re.search(r'fa-map-marker-alt"></i>\s*([^<]+)</a>', chunk)
            make, model = make_model(title)
            out.append({"source": "Cars.ng", "id": u.group(1).rsplit("/", 1)[-1], "title": title.split(" in ")[0],
                        "make": make, "model": model, "year": year_of(title), "price": _num(p.group(1)),
                        "mileage": None, "condition": "", "city": loc.group(1).strip() if loc else "",
                        "transmission": "", "fuel": props[1] if len(props) > 1 else "", "inspected": False,
                        "grade": None, "accident": False, "url": u.group(1)})
        if not new:
            break
        time.sleep(PAUSE)
    return out


SOURCES = {"Autochek": autochek, "Cars.ng": carsng, "Carlots": carlots}


# ---------- pricing ----------

def _key(c: dict) -> str:
    return f"{c['make']} {c['model']}".strip()


def real_km(c: dict) -> float | None:
    """Mileage, unless it's an obvious placeholder (under 20,000 km on a car over 3 years old)."""
    km = c.get("mileage")
    if not km or (c.get("year") and c["year"] <= datetime.now().year - 3 and km < 20_000):
        return None
    return km


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
        prices = sorted(o["price"] for o in pool)
        fair = statistics.median(prices)
        q1, q3 = prices[len(prices) // 4], prices[(3 * len(prices)) // 4]
        if (q3 - q1) / fair > MAX_SPREAD:
            continue                              # prices too mixed (trims vary), can't judge
        miles = [o["mileage"] for o in pool if real_km(o)]
        if real_km(c) and len(miles) >= MIN_COMPS:
            # about 1% of value per 10,000 km above or below the usual mileage, capped at +-15%
            adj = max(-0.15, min(0.15, (statistics.median(miles) - real_km(c)) / 10_000 * 0.01))
            fair *= 1 + adj
        c["fair"], c["comps"] = round(fair, -3), len(pool)
        c["below"] = round((1 - c["price"] / fair) * 100, 1)


def deals(cars: list[dict], top: int = 15, budget: float | None = None) -> list[dict]:
    """Best value: priced under the usual price for similar cars, not accident-flagged.
    Anything more than MAX_BELOW% under is dropped as probably a fake or a part-payment price."""
    ok = [c for c in cars if c["below"] is not None and 5 <= c["below"] <= MAX_BELOW and not c["accident"]
          and (not budget or c["price"] <= budget)]
    ok.sort(key=lambda c: (-(c["below"] + (5 if c["inspected"] else 0)), c["price"]))
    out, seen = [], set()
    for c in ok:                                  # same car posted twice (or on two sites)
        k = (c["make"], c["model"], c["year"], c["price"])
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out[:top]


def deal_text(ds: list[dict], total: int, health: dict) -> str:
    lines = [f"🚗 Car deals today: {len(ds)} best-value cars out of {total:,} listings", ""]
    for i, c in enumerate(ds, 1):
        extra = ", ".join(x for x in (
            f"{real_km(c):,.0f} km" if real_km(c) else "", c["condition"], c["city"],
            "inspected ✓" if c["inspected"] else "") if x)
        lines.append(f"{i}. {html.escape(c['title'])} ({c['year']}): ₦{c['price']:,.0f}, "
                     f"{c['below']:.0f}% under the usual ₦{c['fair']:,.0f} ({c['comps']} similar cars)"
                     + (f"\n   {html.escape(extra)}" if extra else "") + f"\n   {c['url']}")
    lines += ["", "Usual price = middle price of the same model and year (±1) on these sites. "
              "Cheap can mean hidden problems: inspect it, check papers (customs duty, VIN) "
              "and never pay before seeing the car.",
              "Sources: " + ", ".join(f"{k} {v}" for k, v in health.items())]
    return "\n".join(lines)


# ---------- focus: one model, years and area ----------

# area None = all of Nigeria (user's choice 2026-10-10); "Enugu" etc. narrows to one area
FOCUS = {"model": ("toyota", "corolla"), "years": (2006, 2010), "area": None,
         "places": ("enugu", "nsukka", "abakpa", "trans-ekulu", "trans ekulu", "independence layout",
                    "new haven", "ogui", "emene", "agbani", "9th mile", "awkunanaw", "uwani", "achara")}


def focus_extra() -> list[dict]:
    """The model's own pages (and the area's, if one is set), so cars deeper than
    the national pages aren't missed."""
    mk, md = FOCUS["model"]
    jobs = [("Autochek", lambda: autochek(15, f"https://autochek.africa/ng/cars-for-sale/{mk}/{md}")),
            ("Cars.ng", lambda: carsng(30, f"https://cars.ng/{mk}/{mk}-{md}/for-sale"))]
    if FOCUS["area"]:
        area = FOCUS["area"].lower()
        jobs += [("Cars.ng area", lambda: carsng(20, f"https://cars.ng/for-sale/cars-in-{area}")),
                 ("Carlots area", lambda: carlots_region(area))]
    out = []
    for name, fn in jobs:
        try:
            got = fn()
            log.info("focus %s: %d", name, len(got))
            out += got
        except Exception as e:
            log.warning("focus %s failed: %s", name, e)
    return out


def in_focus(c: dict, area: bool = True) -> bool:
    mk, md = FOCUS["model"]
    y0, y1 = FOCUS["years"]
    ok = c["make"] == mk and c["model"] == md and c["year"] and y0 <= c["year"] <= y1
    if area and FOCUS["area"]:
        where = f"{c['city']} {c['title']} {c['url']}".lower()
        ok = ok and any(p in where for p in FOCUS["places"])
    return bool(ok)


def focus_text(cars: list[dict]) -> str:
    mk, md = FOCUS["model"]
    y0, y1 = FOCUS["years"]
    name = f"{mk.title()} {md.title()} {y0}-{y1}"
    local = sorted((c for c in cars if in_focus(c)), key=lambda c: (-(c["below"] or -99), c["price"] or 0))
    allng = [c for c in cars if in_focus(c, area=False) and c["price"]]
    where = FOCUS["area"] or "Nigeria"
    lines = [f"🚗 {name} in {where}: {len(local)} listed today, best value first", ""]
    for i, c in enumerate(local[:15], 1):
        verdict = (f"{c['below']:.0f}% under the usual ₦{c['fair']:,.0f}"
                   + (" ⚠️ suspiciously cheap: could be a deposit price, scam or damaged car" if c["below"] > MAX_BELOW else "")
                   if c["below"] and c["below"] > 0 else
                   f"{-c['below']:.0f}% over the usual ₦{c['fair']:,.0f}" if c["below"] else "not enough similar cars to judge")
        extra = ", ".join(x for x in (f"{real_km(c):,.0f} km" if real_km(c) else "", c["condition"], c["city"]) if x)
        lines.append(f"{i}. {html.escape(c['title'])} ({c['year']}): ₦{c['price']:,.0f}, {verdict}"
                     + (f"\n   {html.escape(extra)}" if extra else "") + f"\n   {c['url']}")
    if not local:
        lines.append(f"None on the sites the bot can read today (Autochek, cars.ng, Carlots). "
                     f"Most {where} cars are sold on Jiji, Facebook and WhatsApp, which block bots.")
    lines += ["", f"Price guide, {name}, all Nigeria today ({len(allng)} listed):"]
    for y in range(y0, y1 + 1):
        ps = sorted(c["price"] for c in allng if c["year"] == y)
        if ps:
            lo, hi = ps[len(ps) // 4], ps[(3 * len(ps)) // 4]
            lines.append(f"• {y}: usual ₦{statistics.median(ps) / 1e6:.1f}M (most between ₦{lo / 1e6:.1f}M and "
                         f"₦{hi / 1e6:.1f}M, {len(ps)} cars)")
    best = deals([c for c in allng if c not in local], top=3) if FOCUS["area"] else []
    if best:
        lines += ["", "Best value elsewhere in Nigeria:"]
        lines += [f"• {html.escape(c['title'])} ({c['year']}), {html.escape(c['city'] or '?')}: ₦{c['price']:,.0f}, "
                  f"{c['below']:.0f}% under usual\n   {c['url']}" for c in best]
    lines += ["", "Use the guide to haggle on Jiji or in person. Foreign-used (tokunbo) usually costs more than "
              "Nigerian-used. Inspect, check customs papers and VIN, never pay before seeing the car."]
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
    seen = {(c["source"], c["id"]) for c in cars}
    cars += [c for c in focus_extra() if (c["source"], c["id"]) not in seen]
    budget = next((float(a.split("=")[1]) for a in sys.argv if a.startswith("--budget=")), None)
    fair_values(cars)
    ds = deals(cars, budget=budget)
    save(cars, ds, health, now)
    print(deal_text(ds, len(cars), health) + "\n")
    text = focus_text(cars)    # the Telegram message: the user's chosen model, years and area
    print(text)
    if "--send" in sys.argv:
        sys.path.insert(0, str(ROOT))
        from bot.alerts import send_telegram
        send_telegram(text)


if __name__ == "__main__":
    main()
