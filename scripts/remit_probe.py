"""One-off check: which UK->Nigeria rate sources can GitHub's servers read?"""
import re
import requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
     "Accept-Language": "en-GB"}
URLS = {
    "kraken_usdtgbp": "https://api.kraken.com/0/public/Ticker?pair=USDTGBP",
    "wise_compare": "https://api.wise.com/v4/comparisons/?sourceCurrency=GBP&targetCurrency=NGN&sendAmount=500",
    "wise_rate": "https://wise.com/rates/live?source=GBP&target=NGN",
    "sendrater": "https://www.sendrater.com/market-rates/GBR-united-kingdom/NGA-nigeria",
    "remitly": "https://www.remitly.com/gb/en/currency-converter/gbp-to-ngn-rate",
    "lemfi": "https://lemfi.com/gb/send-money-to-nigeria",
    "lemfi_rates": "https://lemfi.com/gb/rates",
    "nala": "https://www.nala.com/send-money-to-nigeria",
    "afriex": "https://www.afriex.com/currency-converter/gbp-to-ngn",
    "sendwave": "https://www.sendwave.com/en-gb/send-money-to-nigeria",
    "taptapsend": "https://www.taptapsend.com/send-money-to-nigeria",
    "africhange": "https://africhange.com/",
    "nairacompare": "https://nairacompare.ng/send-money",
    "er_api": "https://open.er-api.com/v6/latest/GBP",
}
for name, url in URLS.items():
    try:
        r = requests.get(url, headers=H, timeout=20)
        body = r.text
        nums = sorted(set(re.findall(r"1[,]?[78]\d\d\.\d{1,4}", body)))[:12]
        print(f"{name}: HTTP {r.status_code}, {len(body)} bytes, naira-like numbers {nums}")
        if name in ("kraken_usdtgbp", "wise_compare", "wise_rate", "er_api"):
            print("   ", body[:600].replace("\n", " "))
    except Exception as e:
        print(f"{name}: failed {type(e).__name__}: {e}")
