"""One-off check: which more Nigerian car sites can GitHub's servers read,
and how does Carlots number its pages?"""
import re

import requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36", "Accept-Language": "en-NG,en;q=0.9"}


def get(u):
    try:
        r = requests.get(u, headers=H, timeout=25, allow_redirects=True)
        prices = re.findall(r"(?:₦|&#8358;|NGN|N)\s?[\d,]{7,}", r.text)
        print(f"\n== {u} -> {r.url}\n   HTTP {r.status_code} {len(r.text):,} chars cf={'cf-ray' in r.headers} "
              f"prices={len(prices)} {prices[:3]}", flush=True)
        return r.text
    except Exception as e:
        print(f"\n== {u}\n   failed {type(e).__name__} {str(e)[:120]}", flush=True)
        return ""


h = get("https://carlots.ng/")
print("   carlots links:", sorted(set(re.findall(r'href="(https://carlots\.ng/[^"#]*)"', h)))[:60])
print("   pagination:", re.findall(r'href="([^"]*(?:page|pg|/p/)[^"]*)"', h)[:15])
for u in ("https://carlots.ng/search", "https://carlots.ng/cars", "https://carlots.ng/category/cars",
          "https://carlots.ng/search?page=2", "https://carlots.ng/?page=2", "https://carlots.ng/page/2"):
    h2 = get(u)
    print("   titles:", len(re.findall(r'<a class="title"', h2)))

for u in ("https://www.naijauto.com/", "https://www.naijauto.com/cars-for-sale", "https://cars.ng/",
          "https://www.carsnigeria.com/", "https://autobazaar.ng/", "https://www.motormata.com/",
          "https://nigeria.carsales.com/", "https://www.cheki.com.ng/", "https://www.mycarlot.ng/",
          "https://www.carvillenigeria.com/", "https://coscharis.com/", "https://www.kilimall.ng/",
          "https://www.jumia.com.ng/cars/", "https://www.olx.com.ng/", "https://carmart.ng/",
          "https://www.nairaland.com/autos", "https://autoxpress.ng/", "https://www.carworld.ng/"):
    h = get(u)
    if h:
        print("   links:", sorted(set(re.findall(r'href="(/[a-z][^"#?]{3,50})"', h)))[:15])
