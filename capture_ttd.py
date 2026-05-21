import httpx

BASE = "https://www.theatreticketsdirect.co.uk"
SHOW_ID = 7158
REFERER = f"{BASE}/shows/{SHOW_ID}/a-midsummer-night%E2%80%99s-dream---globe-theatre-tickets"

with httpx.Client(
    headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9",
    },
    follow_redirects=True,
) as c:
    warm = c.get(REFERER, timeout=20)
    print(f"warmup: HTTP {warm.status_code}, cookies={list(c.cookies.keys())}")

    r = c.post(
        f"{BASE}/show/bindcalendar",
        data={"id": str(SHOW_ID), "month": "5", "year": "2026"},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": REFERER,
            "Accept": "text/html, */*; q=0.01",
        },
        timeout=20,
    )
    print(f"bindcalendar: HTTP {r.status_code}, {len(r.text)} bytes")
    with open("ttd_cal_sample.html", "w", encoding="utf-8") as f:
        f.write(r.text)
    print("Wrote ttd_cal_sample.html")
