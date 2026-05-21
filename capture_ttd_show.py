import httpx

BASE = "https://www.theatreticketsdirect.co.uk"
SHOW_ID = 7158
SHOW_URL = f"{BASE}/shows/{SHOW_ID}/a-midsummer-night%E2%80%99s-dream---globe-theatre-tickets"

with httpx.Client(
    headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9",
    },
    follow_redirects=True,
) as c:
    r = c.get(SHOW_URL, timeout=30)
    print(f"show page: HTTP {r.status_code}, {len(r.text)} bytes")

    # Quick sanity check: does the response contain price markers?
    import re
    prices = re.findall(r"£\s*\d+(?:\.\d{2})?", r.text)
    print(f"price tokens found: {len(prices)}")
    if prices:
        from collections import Counter
        common = Counter(prices).most_common(5)
        print(f"top 5 prices: {common}")
    perf_urls = re.findall(r"/shows/seats/\d+/\d{4}/\d{1,2}/\d{1,2}/\d+/\d{1,2}-\d{2}/\d+", r.text)
    print(f"perf URLs found: {len(perf_urls)} (first 3: {perf_urls[:3]})")

    with open("ttd_show_sample.html", "w", encoding="utf-8") as f:
        f.write(r.text)
    print("Wrote ttd_show_sample.html")
