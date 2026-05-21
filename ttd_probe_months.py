import httpx, re
from collections import Counter

BASE = "https://www.theatreticketsdirect.co.uk"
SHOW_ID = 7158
REFERER = f"{BASE}/shows/{SHOW_ID}/a-midsummer-night%E2%80%99s-dream---globe-theatre-tickets"

with httpx.Client(
    headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9",
    },
    follow_redirects=True,
) as c:
    c.get(REFERER, timeout=20)

    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE,
        "Referer": REFERER,
        "Accept": "text/html, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    for monthyear_value in ["", "6,2026", "7,2026", "8,2026"]:
        r = c.post(
            f"{BASE}/show/bindcalendar",
            data={"id": str(SHOW_ID), "monthyear": monthyear_value, "tickets": "2", "loadMonths": "true"},
            headers=headers,
            timeout=20,
        )
        urls = re.findall(r"/shows/seats/\d+/(\d{4})/(\d{1,2})/\d{1,2}/\d+/\d{1,2}-\d{2}/(\d+)", r.text)
        non_zero = [u for u in urls if u[2] != "0"]
        prices = re.findall(r"&#163;(\d+\.\d{2})", r.text)
        months = Counter((y, m) for y, m, _ in non_zero)
        print(f"monthyear={monthyear_value!r:12s}  HTTP {r.status_code}  size {len(r.text):>5}  perfs {len(non_zero):>3}  prices {len(prices):>3}  months: {dict(months)}")
