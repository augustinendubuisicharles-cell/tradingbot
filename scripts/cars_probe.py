"""One-off check: what structure do the readable Nigerian car sites use?"""
import json
import re

import requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36", "Accept-Language": "en-NG,en;q=0.9"}


def get(u):
    r = requests.get(u, headers=H, timeout=25)
    print(f"\n== {u}\n   HTTP {r.status_code} {len(r.text):,} chars", flush=True)
    return r.text


def walk(o, path="", depth=0):
    """Prints the shape of JSON down to lists of dicts that look like car listings."""
    if depth > 7:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            walk(v, f"{path}.{k}", depth + 1)
    elif isinstance(o, list) and o and isinstance(o[0], dict):
        keys = list(o[0].keys())
        if any(k.lower() in ("price", "marketplaceprice", "title", "year", "mileage") for k in keys):
            print(f"   LIST {path} len={len(o)} keys={keys[:40]}")
            print("   SAMPLE", json.dumps(o[0])[:1200])


html = get("https://autochek.africa/ng/cars-for-sale")
m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
if m:
    walk(json.loads(m.group(1)))
else:
    print("   no __NEXT_DATA__; json-ish snippets:", re.findall(r'.{80}"price".{120}', html)[:3])
html = get("https://autochek.africa/ng/cars-for-sale?page_number=2")
print("   page 2 different:", "page_number" in html)

html = get("https://www.carmart.ng/")
links = sorted(set(re.findall(r'href="(https://www\.carmart\.ng/[^"#?]+)"', html)))
print("   links", len(links), links[:40])
for snip in re.findall(r'.{300}₦5,700,000.{300}', html, re.S)[:1]:
    print("   AROUND PRICE", re.sub(r"\s+", " ", snip))

for u in ("https://www.cars45.com/", "https://www.cars45.com/listing/", "https://www.cars45.com/buy-cars"):
    try:
        h = get(u)
        print("   prices", re.findall(r"₦\s?[\d,]{6,}", h)[:4],
              "links", sorted(set(re.findall(r'href="(/[a-z][^"#?]{3,40})"', h)))[:25])
    except Exception as e:
        print("   failed", e)
