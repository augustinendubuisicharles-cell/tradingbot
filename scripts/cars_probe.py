"""One-off check: can GitHub's servers read Nigerian car listing sites?
Prints status, size, robots.txt rules and a sample of what looks like prices."""
import re

import requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36", "Accept-Language": "en-NG,en;q=0.9"}
URLS = [
    "https://jiji.ng/robots.txt",
    "https://jiji.ng/cars",
    "https://jiji.ng/api_web/v1/listing?slug=cars&page=1",
    "https://jiji.ng/api_web/v1/listing?slug=cars&init_page=true&page=1&webp=true",
    "https://www.cars45.com/robots.txt",
    "https://www.cars45.com/listing",
    "https://autochek.africa/robots.txt",
    "https://autochek.africa/ng/cars-for-sale",
    "https://api.autochek.africa/v1/inventory/car/search?country=NG&page_number=1&page_size=5",
    "https://www.carmart.ng/",
]
for u in URLS:
    try:
        r = requests.get(u, headers=H, timeout=25)
        body = r.text
        prices = re.findall(r"₦\s?[\d,]{6,}|\"price\"\s*:\s*\"?[\d.]{6,}", body)[:4]
        print(f"\n== {u}\n   HTTP {r.status_code}  {len(body):,} chars  type {r.headers.get('content-type', '')[:40]}"
              f"  cf={'cf-ray' in r.headers}  prices {prices}", flush=True)
        if u.endswith("robots.txt"):
            print("   " + "\n   ".join(l for l in body.splitlines()[:25] if l.strip()))
        elif "json" in r.headers.get("content-type", ""):
            print("   " + body[:700])
    except Exception as e:
        print(f"\n== {u}\n   failed: {type(e).__name__} {str(e)[:150]}", flush=True)
