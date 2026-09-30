"""Client for Grab share-location API (no auth needed)."""
import re
import time
import urllib.request
import urllib.error
import json

API_BASE = "https://api.grab.com"
UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")


def resolve_token(link: str) -> str | None:
    """Extract the share token from a sharelocation.grab.com or app.grab.com/s link."""
    link = (link or "").strip()
    m = re.search(r"sharelocation\.grab\.com/o/([A-Za-z0-9_-]+)", link)
    if m:
        return m.group(1)
    m = re.search(r"app\.grab\.com/s/([A-Za-z0-9]+)", link)
    if not m:
        return None
    # Grab's shortlink endpoint intermittently serves the SPA (HTTP 200)
    # instead of the 302 redirect, so retry a few times. Follow redirects
    # and read the token from the final URL's shareOrderLink parameter.
    for _ in range(4):
        try:
            req = urllib.request.Request(link, headers={"User-Agent": UA}, method="GET")
            with urllib.request.urlopen(req, timeout=20) as resp:
                final = resp.geturl()
            m2 = (re.search(r"sharelocation\.grab\.com/o/([A-Za-z0-9_-]+)", final)
                  or re.search(r"[?&]shareOrderLink=([A-Za-z0-9]+)", final))
            if m2:
                return m2.group(1)
        except Exception:
            pass
        time.sleep(1.5)
    return None


def fetch_details(token: str) -> tuple[dict | None, str]:
    """Fetch booking details for a share token.

    Returns (data, status):
      - "ok"    : API menjawab dan sesi pantauan ACTIVE
      - "dead"  : link kedaluwarsa / tidak valid (API menjawab demikian / HTTP 4xx)
      - "error" : gangguan jaringan atau HTTP 5xx (transient, layak dicoba lagi)
    """
    url = f"{API_BASE}/api/v1/safety/sharemyride/{token}/bookingdetails?fullData=false"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            return None, "dead"
        return None, "error"
    except Exception:
        return None, "error"
    if not data or not data.get("pass") or data.get("sessionStatus") != "ACTIVE":
        return data, "dead"
    return data, "ok"


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    from math import radians, sin, cos, asin, sqrt
    R = 6371.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))


if __name__ == "__main__":
    tok = resolve_token("https://app.grab.com/s/sP01AQuZ")
    print("token:", tok)
    d, status = fetch_details(tok)
    if d:
        drv = d.get("driver", {})
        bk = d.get("booking", {})
        print("status:", status, "| pass:", (d or {}).get("pass"), "| session:", (d or {}).get("sessionStatus"),
              "| state:", bk.get("bookingState"))
        print("driver:", drv.get("name"), drv.get("rating"), drv.get("vehicleModel"),
              drv.get("vehiclePlateNumber"))
        print("loc:", (drv.get("location") or {}).get("latitude"),
              (drv.get("location") or {}).get("longitude"))
        print("pickup:", (bk.get("pickup") or {}).get("keywords"))
        print("dropoff:", (bk.get("dropOff") or {}).get("keywords"))
        print("title:", (d.get("messageStatus") or {}).get("title"))
        print("ETA:", d.get("route", {}).get("ETA"),
              time.strftime("%H:%M", time.localtime(d.get("route", {}).get("ETA") or 0)))
