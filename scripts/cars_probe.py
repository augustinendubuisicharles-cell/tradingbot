"""One-off check: one full listing card from cars.ng and carlots.ng."""
import re

import requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36", "Accept-Language": "en-NG,en;q=0.9"}
h = requests.get("https://cars.ng/for-sale?page=2", headers=H, timeout=30).text
i = h.find("fs-6 mb-0 fw-bold")
print("CARS.NG CARD:", re.sub(r"\s+", " ", h[i - 3500:i + 1500]))
print("cards on page:", h.count("fs-6 mb-0 fw-bold"))
h = requests.get("https://carlots.ng/cars/toyota-for-sale", headers=H, timeout=30).text
print("\nCARLOTS toyota titles:", h.count('<a class="title"'))
i = h.find('<a class="title"')
print("CARLOTS CARD:", re.sub(r"\s+", " ", h[i - 1500:i + 2000]))
