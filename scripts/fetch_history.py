"""Download years of free daily price history for strategy research.

Source: data.binance.vision (Binance's public bulk archive, no key). It also
keeps coins that were later delisted, so tests are not limited to today's
survivors. Also grabs perpetual funding rates (used only as a sentiment
signal) and the Fear & Greed history.

Writes:
  research/daily.csv.gz    symbol,date,open,high,low,close,quote_volume
  research/funding.csv.gz  symbol,date,funding (daily sum of 8h rates)
  research/fng.csv         date,value
"""
import csv
import gzip
import io
import json
import re
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree

BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
FILES = "https://data.binance.vision"
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
SKIP = re.compile(r"^(USDC|FDUSD|TUSD|BUSD|USDP|DAI|EUR|GBP|AEUR|PAXG|WBTC|WBETH|USDE|XUSD|BFUSD|USD1|\w+(UP|DOWN|BULL|BEAR))USDT$")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "research")


def get(url: str, tries: int = 6) -> bytes | None:
    for k in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
        time.sleep(2 ** k)
    return None


def listing(prefix: str, folders: bool = True) -> list[str]:
    out, marker = [], ""
    while True:
        url = f"{BUCKET}?delimiter=/&prefix={prefix}" + (f"&marker={marker}" if marker else "")
        blob = get(url)
        if blob is None:
            raise RuntimeError(f"listing failed: {prefix}")
        root = ElementTree.fromstring(blob)
        if folders:
            items = [p.find(f"{NS}Prefix").text for p in root.findall(f"{NS}CommonPrefixes")]
        else:
            items = [c.find(f"{NS}Key").text for c in root.findall(f"{NS}Contents")]
        out += items
        if root.find(f"{NS}IsTruncated").text != "true" or not items:
            return out
        marker = (root.find(f"{NS}NextMarker").text if root.find(f"{NS}NextMarker") is not None else items[-1])


def rows_from_zip(blob: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = z.read(z.namelist()[0]).decode()
    return [line.split(",") for line in text.splitlines() if line and line[0].isdigit()]


def ms(v: str) -> int:
    t = int(v)
    return t // 1000 if t > 10**14 else t   # 2025+ files use microseconds


DONE = [0]


def safe(fn):
    """One coin failing must not stop the whole download."""
    def run(symbol):
        try:
            return fn(symbol)
        except Exception as e:
            print(f"skipped {symbol}: {e}", flush=True)
            return []
        finally:
            DONE[0] += 1
            if DONE[0] % 50 == 0:
                print(f"{DONE[0]} coins done", flush=True)
    return run


def daily(symbol: str) -> list[tuple]:
    keys = [k for k in listing(f"data/spot/monthly/klines/{symbol}/1d/", folders=False) if k.endswith(".zip")]
    out = {}
    for k in keys:
        blob = get(f"{FILES}/{k}")
        if not blob:
            continue
        for r in rows_from_zip(blob):
            day = time.strftime("%Y-%m-%d", time.gmtime(ms(r[0]) / 1000))
            out[day] = (symbol[:-4], day, r[1], r[2], r[3], r[4], r[7])
    # this month is not archived monthly yet: add daily files
    for k in [k for k in listing(f"data/spot/daily/klines/{symbol}/1d/", folders=False) if k.endswith(".zip")][-40:]:
        blob = get(f"{FILES}/{k}")
        for r in rows_from_zip(blob) if blob else []:
            day = time.strftime("%Y-%m-%d", time.gmtime(ms(r[0]) / 1000))
            out.setdefault(day, (symbol[:-4], day, r[1], r[2], r[3], r[4], r[7]))
    return [out[d] for d in sorted(out)]


def funding(symbol: str) -> list[tuple]:
    keys = [k for k in listing(f"data/futures/um/monthly/fundingRate/{symbol}/", folders=False) if k.endswith(".zip")]
    days: dict[str, float] = {}
    for k in keys:
        blob = get(f"{FILES}/{k}")
        if not blob:
            continue
        for r in rows_from_zip(blob):
            day = time.strftime("%Y-%m-%d", time.gmtime(ms(r[0]) / 1000))
            days[day] = days.get(day, 0.0) + float(r[2])
    return [(symbol[:-4], d, f"{v:.6f}") for d, v in sorted(days.items())]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    symbols = [p.rstrip("/").split("/")[-1] for p in listing("data/spot/monthly/klines/")]
    symbols = [s for s in symbols if s.endswith("USDT") and not SKIP.match(s)]
    print(f"{len(symbols)} USDT spot symbols in the archive", flush=True)
    with ThreadPoolExecutor(10) as ex:
        results = list(ex.map(safe(daily), symbols))
    n = 0
    with gzip.open(OUT / "daily.csv.gz", "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "date", "open", "high", "low", "close", "quote_volume"])
        for rows in results:
            w.writerows(rows)
            n += len(rows)
    print(f"daily rows: {n}", flush=True)

    perps = {p.rstrip("/").split("/")[-1] for p in listing("data/futures/um/monthly/fundingRate/")}
    fsyms = [s for s in symbols if s in perps]
    DONE[0] = 0
    with ThreadPoolExecutor(10) as ex:
        fres = list(ex.map(safe(funding), fsyms))
    with gzip.open(OUT / "funding.csv.gz", "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "date", "funding"])
        for rows in fres:
            w.writerows(rows)
    print(f"funding symbols: {len(fsyms)}", flush=True)

    blob = get("https://api.alternative.me/fng/?limit=0&format=json")
    if blob:
        data = json.loads(blob)["data"]
        with open(OUT / "fng.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "value"])
            for d in sorted(data, key=lambda d: int(d["timestamp"])):
                w.writerow([time.strftime("%Y-%m-%d", time.gmtime(int(d["timestamp"]))), d["value"]])


if __name__ == "__main__":
    main()
