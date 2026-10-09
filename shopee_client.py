"""Client for ShopeeFood share-tracker API (no auth needed).

Reverse-engineered from the tracker web app
(https://www.shopeefood.co.id/tracker):
  GET /api/buyer/orders/{order_id}/tracing/{code}
  -> {"code": 0, "msg": "success",
      "data": {"order": {...}, "payment": [...], ...}}

Error codes (from the web app): 1000 ParamInvalid, 2001 RecordNotFound,
11160051 ShareTokenInvalid, 11160052 LinkIsExpired.
"""
import json
import urllib.request
import urllib.error
from urllib.parse import urlparse, parse_qsl

API_BASE = "https://www.shopeefood.co.id"
UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")

# Kode status pesanan (dilihat dari web app tracker ShopeeFood)
STATUS_LABEL = {
    300: "disiapkan restoran",   # Confirmed
    400: "driver ditugaskan",    # Assigned
    411: "diproses",             # EnterProcess
    412: "siap diambil",         # ToCollect
    425: "dikonfirmasi",         # Reconfirmed
    430: "diambil driver",       # Picked
    431: "diantar driver",       # EnrouteDelivery
}
# Fase driver membawa pesanan (boleh hitung progres + notifikasi dekat/macets)
DELIVERY_STATES = {430, 431}
# Kode error API yang berarti link mati/tidak valid
DEAD_CODES = {1000, 2001, 11160051, 11160052}

AMOUNT_DIV = 100000  # nominal API dalam satuan 1/100rb rupiah


def parse_link(link):
    """Ambil (order_id, code) dari link tracker ShopeeFood.

    Menerima varian param orderId|id dan code|shareCode (lihat web app).
    Mengembalikan None bila bukan link tracker ShopeeFood.
    """
    link = (link or "").strip()
    if "shopeefood.co.id/tracker" not in link:
        return None
    try:
        qs = dict(parse_qsl(urlparse(link).query, keep_blank_values=True))
    except Exception:
        return None
    order_id = qs.get("orderId") or qs.get("id")
    code = qs.get("code") or qs.get("shareCode")
    if not (order_id and code):
        return None
    return order_id, code


def fetch_details(order_id, code):
    """Ambil detail pesanan dari API tracker.

    Returns (data, status):
      - "ok"    : API menjawab code 0 dan data order valid
      - "dead"  : link kedaluwarsa / tidak valid (kode error API / HTTP 4xx)
      - "error" : gangguan jaringan atau HTTP 5xx (transient, layak dicoba lagi)

    `code` disambung mentah ke path (tanpa encoding), sama seperti web app.
    """
    url = f"{API_BASE}/api/buyer/orders/{order_id}/tracing/{code}"
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, "dead" if 400 <= e.code < 500 else "error"
    except Exception:
        return None, "error"
    if not isinstance(body, dict):
        return None, "error"
    api_code = body.get("code")
    if api_code != 0:
        return body, "dead" if api_code in DEAD_CODES else "error"
    data = body.get("data")
    if not data or not (data.get("order") or {}).get("id"):
        return data, "dead"
    return data, "ok"


def order_of(data):
    return (data or {}).get("order") or {}


def _truthy_ts(v):
    try:
        return int(str(v)) > 0
    except (TypeError, ValueError):
        return False


def is_done(order):
    """Pesanan selesai/diantar: complete_time / delivery_complete_time terisi."""
    return _truthy_ts(order.get("complete_time")) or \
        _truthy_ts(order.get("delivery_complete_time"))


def is_cancelled(order):
    return _truthy_ts(order.get("cancel_time"))


def status_code(order):
    try:
        return int(order.get("status") or 0)
    except (TypeError, ValueError):
        return 0


def status_label(code):
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "diproses"
    return STATUS_LABEL.get(code, "diproses")


def driver_of(data):
    """Objek driver mentah. Web app mengambil dari order.driver dengan
    fallback ke unionDelivery(.union_delivery).driver — sama seperti di sini."""
    o = order_of(data)
    drv = o.get("driver")
    if drv:
        return drv
    for key in ("unionDelivery", "union_delivery"):
        ud = (data or {}).get(key) or o.get(key) or {}
        if isinstance(ud, dict) and ud.get("driver"):
            return ud["driver"]
    return {}


def driver_name(driver):
    return (driver or {}).get("full_name") or (driver or {}).get("fullName") or ""


def driver_phone(driver):
    d = driver or {}
    return d.get("phone") or d.get("phone_no") or d.get("phoneNo") or ""


def driver_rating(driver):
    d = driver or {}
    return d.get("rating") or d.get("rating_score") or ""


def driver_plate(driver):
    d = driver or {}
    return d.get("vehicle_plate_no") or d.get("vehiclePlateNo") or ""


def driver_vehicle(driver):
    d = driver or {}
    return d.get("vehicle_description") or d.get("vehicleDescription") or ""


def _latlng(loc):
    try:
        lat, lng = float(loc.get("latitude")), float(loc.get("longitude"))
    except (TypeError, ValueError, AttributeError):
        return None
    if lat == 0 and lng == 0:
        return None
    return lat, lng


def driver_loc(driver):
    return _latlng((driver or {}).get("location") or {})


def pickup_geo(data):
    """Titik pickup (resto) dari union_delivery — fallback posisi driver
    seperti web app (De.location || unionDelivery.deliveryOrder.geoTracking.pickupGeo)."""
    o = order_of(data)
    for ukey, dkey, gkey in (("unionDelivery", "deliveryOrder", "geoTracking"),
                             ("union_delivery", "delivery_order", "geo_tracking")):
        ud = (data or {}).get(ukey) or o.get(ukey) or {}
        if not isinstance(ud, dict):
            continue
        gt = (ud.get(dkey) or {}).get(gkey) or {}
        ll = _latlng(gt.get("pickup_geo") or gt.get("pickupGeo") or {})
        if ll:
            return ll
    return None


def store_loc(order):
    return _latlng((order.get("store") or {}).get("location") or {})


def dest_loc(order):
    return _latlng((order.get("delivery_address") or {}).get("location") or {})


def rupiah(v):
    """Nominal API -> rupiah (dibulatkan)."""
    try:
        return int(str(v)) // AMOUNT_DIV
    except (TypeError, ValueError):
        return 0


def fmt_rp(v):
    return f"Rp{rupiah(v):,}".replace(",", ".")


def eta_range(order):
    """(min_ts, max_ts) estimasi tiba dalam detik epoch; (None, None) bila tak ada."""
    d = order.get("display_estimate_delivered_time") or {}
    try:
        lo, hi = int(d.get("min") or 0), int(d.get("max") or 0)
    except (TypeError, ValueError):
        return None, None
    return (lo // 1000) if lo > 0 else None, (hi // 1000) if hi > 0 else None


def item_names(order, limit=4):
    """['1x Risol Mentai', ...] untuk ringkasan kartu."""
    names = []
    for it in order.get("items") or []:
        try:
            dish = ((it.get("cart_item") or {}).get("detail") or {}).get("dish") or {}
            nm = dish.get("name")
        except AttributeError:
            nm = None
        if nm:
            names.append(f"{it.get('quantity') or 1}x {nm}")
        if len(names) >= limit:
            break
    return names


def tracker_url(order_id, code):
    return f"{API_BASE}/tracker?code={code}&orderId={order_id}"


if __name__ == "__main__":
    import sys
    from datetime import datetime
    from zoneinfo import ZoneInfo
    link = sys.argv[1] if len(sys.argv) > 1 else ""
    parsed = parse_link(link)
    print("parsed:", parsed)
    if parsed:
        d, status = fetch_details(*parsed)
        o = order_of(d)
        print("status:", status, "| order_status:", o.get("status"),
              status_label(o.get("status")))
        print("store:", (o.get("store") or {}).get("name"))
        print("items:", item_names(o))
        print("driver:", driver_name(driver_of(d)) or "(belum ada)")
        lo, hi = eta_range(o)
        wib = ZoneInfo("Asia/Jakarta")
        print("eta:", datetime.fromtimestamp(lo, wib).strftime("%H:%M") if lo else "-",
              "-", datetime.fromtimestamp(hi, wib).strftime("%H:%M") if hi else "")
        print("done:", is_done(o), "| cancelled:", is_cancelled(o))
