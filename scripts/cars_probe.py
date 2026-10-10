"""One-off check: page structure of carlots.ng/cars and cars.ng/for-sale."""
import json
import re

import requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36", "Accept-Language": "en-NG,en;q=0.9"}


def get(u):
    r = requests.get(u, headers=H, timeout=30)
    print(f"\n== {u} -> {r.url}\n   HTTP {r.status_code} {len(r.text):,} chars", flush=True)
    return r.text


def walk(o, path="", depth=0):
    if depth > 8:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            walk(v, f"{path}.{k}", depth + 1)
    elif isinstance(o, list) and o and isinstance(o[0], dict):
        keys = list(o[0].keys())
        if any(k.lower() in ("price", "amount", "year", "mileage", "title", "name") for k in keys) and len(o) > 5:
            print(f"   LIST {path} len={len(o)} keys={keys[:45]}")
            print("   SAMPLE", json.dumps(o[0])[:1500])


for u in ("https://carlots.ng/cars?page=2", "https://carlots.ng/cars?page=3", "https://carlots.ng/cars/page-2"):
    h = get(u)
    t = re.findall(r'<a class="title" href="([^"]+)"', h)
    print("   titles", len(t), t[:2])
h = get("https://carlots.ng/cars")
print("   paging links:", sorted(set(re.findall(r'href="([^"]*cars[^"]*(?:page|pg|p=)[^"]*)"', h)))[:10])
print("   any page=:", sorted(set(re.findall(r'href="([^"]*page[^"]*)"', h)))[:10])

for u in ("https://cars.ng/for-sale", "https://cars.ng/for-sale?page=2"):
    h = get(u)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
    if m:
        walk(json.loads(m.group(1)))
    else:
        print("   no NEXT_DATA; scripts:", re.findall(r'<script[^>]*src="([^"]+)"', h)[:5])
        for s in re.findall(r'.{250}₦[\d,]{7,}.{250}', h, re.S)[:2]:
            print("   AROUND", re.sub(r"\s+", " ", s))
        print("   ld+json:", [x[:600] for x in re.findall(r'<script type="application/ld\+json">(.*?)</script>', h, re.S)[:2]])
        print("   page links:", sorted(set(re.findall(r'href="([^"]*page[^"]*)"', h)))[:10])
