import httpx, re
from collections import Counter

BASE = "https://www.theatreticketsdirect.co.uk"
SHOW_ID = 7158
REFERER = f"{BASE}/shows/{SHOW_ID}/a-midsummer-night%E2%80%99s-dream---globe-theatre-tickets"
CAL_URL = f"{BASE}/calendar/{SHOW_ID}/5,2026"

with httpx.Client(
    headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9",
    },
    follow_redirects=True,
) as c:
    warm = c.get(REFERER, timeout=20)
    print(f"warmup: HTTP {warm.status_code}")

    r = c.get(CAL_URL, timeout=30, headers={"Referer": REFERER})
    print(f"calendar page: HTTP {r.status_code}, {len(r.text)} bytes")

    prices = re.findall(r"(?:£|&pound;|&#163;)\s*\d+(?:\.\d{2})?", r.text)
    print(f"price tokens (£): {len(prices)}; top 5: {Counter(prices).most_common(5)}")
    perf_urls = re.findall(r"/shows/seats/\d+/\d{4}/\d{1,2}/\d{1,2}/\d+/\d{1,2}-\d{2}/\d+", r.text)
    real_ids = [u for u in perf_urls if not u.endswith("/0")]
    print(f"perf URLs: {len(perf_urls)} ({len(real_ids)} with real (non-zero) IDs)")
    if real_ids:
        print(f"  first 3 with real IDs: {real_ids[:3]}")

    with open("ttd_calendar_sample.html", "w", encoding="utf-8") as f:
        f.write(r.text)
    print("Wrote ttd_calendar_sample.html")
