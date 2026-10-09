#!/usr/bin/env python3
"""Pantau GrabFood — bot Telegram pribadi pemantau pesanan GrabFood.

Tempel link share Grab (app.grab.com/s/... atau sharelocation.grab.com/o/...),
bot memantau posisi driver tiap 10 detik lewat live location + kartu status,
lalu memberi tahu saat driver jalan, sudah dekat, dan pesanan selesai.
"""
import json
import os
import re
import time
import html as htmlmod
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

WIB = ZoneInfo("Asia/Jakarta")  # semua waktu tampil pakai WIB

import tg
from grab_client import resolve_token, fetch_details, haversine_km
import shopee_client as sp

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE, "state.json")
LOG_PATH = os.path.join(BASE, "bot.log")
HISTORY_PATH = os.path.join(BASE, "history.json")
HISTORY_MAX = 100

POLL_INTERVAL = 10          # detik antar cek Grab API
MAX_TRACKINGS = 3           # maks pantauan bersamaan
MAX_FETCH_FAILS = 12        # 12x gagal fetch (~2 mnt) baru pantauan dihentikan
MILESTONES = (0.25, 0.5, 0.8)  # notifikasi progres perjalanan driver
NEAR_THRESHOLD_KM = 0.30    # ambang "driver sudah dekat"
MAX_TRACK_MINUTES = 120     # batas durasi pantauan
STUCK_MINUTES = 6           # ambang "driver berhenti lama" (menit)
STUCK_DIST_KM = 0.10        # dianggap diam bila bergerak < 100 m
RATING_REMINDER_MINUTES = 10  # jeda pengingat rating setelah selesai

STATE_LABEL = {
    "ORDER_IN_PREPARE": "disiapkan restoran",
    "PICKING_UP": "driver mengambil pesanan",
    "ORDER_EXECUTING": "diantar driver",
    "ORDER_EXECUTED": "tiba di tujuan",
    "COMPLETED": "selesai",
}
CANCELLED = {"CANCELLED_DRIVER", "CANCELLED_MAX", "CANCELLED_MERCHANT",
             "CANCELLED_OPERATOR", "CANCELLED_PASSENGER", "CANCELLED_SHOPPER"}


def log(msg):
    # stdout sudah dialihkan ke bot.log oleh watchdog (nohup), jadi cukup print
    print(f"{datetime.now(WIB).strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def load_state():
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
    except Exception:
        s = {"owner_id": None, "offset": 0, "next_id": 1,
             "trackings": {}, "awaiting_link": False}
    s.setdefault("reminders", [])
    s.setdefault("settings", {})
    s["settings"].setdefault("rating_reminder", True)
    return s


def load_history():
    try:
        with open(HISTORY_PATH) as f:
            h = json.load(f)
            return h if isinstance(h, list) else []
    except Exception:
        return []


def save_history(h):
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(h[-HISTORY_MAX:], f, ensure_ascii=False)
    os.replace(tmp, HISTORY_PATH)


def record_history(t, d=None, ended="completed"):
    """Catat pesanan yang selesai ke riwayat.

    d boleh None (mis. link kedaluwarsa tanpa data terbaru) — nama driver
    diambil dari data pantauan. ended: "completed" | "expired".
    """
    plat = t.get("platform", "grab")
    if plat == "shopee":
        drv = sp.driver_of(d)
        drv_name = sp.driver_name(drv) or t.get("driver", "-")
        try:
            rating = float(sp.driver_rating(drv))
        except (TypeError, ValueError):
            rating = None
    else:
        drv = (d.get("driver") if d else None) or {}
        drv_name = drv.get("name") or t.get("driver", "-")
        try:
            rating = float(drv.get("rating"))
        except (TypeError, ValueError):
            rating = None
    now = time.time()
    eta_first = t.get("eta_first")
    entry = {
        "platform": "shopeefood" if plat == "shopee" else "grabfood",
        "merchant": t.get("merchant", "-"),
        "dropoff": t.get("dropoff", "-"),
        "driver": drv_name,
        "rating": rating,
        "started_ts": t.get("started", now),
        "completed_ts": now,
        "duration_min": round((now - t.get("started", now)) / 60, 1),
        "eta_diff_min": round((now - eta_first) / 60, 1) if eta_first else None,
        "ended": ended,
    }
    h = load_history()
    h.append(entry)
    save_history(h)
    return entry


def save_state(s):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def esc(t):
    return htmlmod.escape(str(t or "-"))


def fmt_dist(km):
    if km < 1:
        return f"{int(km * 1000)} m"
    return f"{km:.1f} km"


def fmt_eta(ts):
    try:
        return datetime.fromtimestamp(int(ts), WIB).strftime("%H:%M")
    except Exception:
        return "-"


def progress_bar(frac):
    """Bar 10 blok untuk progres perjalanan driver."""
    frac = max(0.0, min(1.0, frac))
    full = int(round(frac * 10))
    return "🟩" * full + "⬜" * (10 - full) + f" {int(frac * 100)}%"


def card_text(d, t):
    drv = d.get("driver") or {}
    bk = d.get("booking") or {}
    pk = bk.get("pickup") or {}
    do = bk.get("dropOff") or {}
    ms = d.get("messageStatus") or {}
    pm = ms.get("processMsg") or {}
    dloc = drv.get("location") or {}
    doloc = do.get("location") or {}
    dist = None
    if dloc.get("latitude") and doloc.get("latitude"):
        dist = haversine_km(dloc["latitude"], dloc["longitude"],
                            doloc["latitude"], doloc["longitude"])
    state = bk.get("bookingState", "")
    label = STATE_LABEL.get(state, state.replace("_", " ").lower())
    mins = int((time.time() - t["started"]) // 60)
    lines = [
        f"🛵 <b>{esc(ms.get('title') or 'Memantau pesanan')}</b>",
    ]
    if pm.get("message"):
        lines.append(f"<i>{esc(pm['message'])}</i>")
    lines += [
        "",
        f"👤 {esc(drv.get('name'))} (⭐{drv.get('rating') or '-'})",
        f"🏍 {esc(drv.get('vehicleModel'))} · <code>{esc(drv.get('vehiclePlateNumber'))}</code>",
        f"🍜 Dari: {esc(pk.get('keywords'))}",
        f"📍 Ke: {esc(do.get('keywords'))}",
        f"📊 Status: {esc(label)}",
    ]
    if dist is not None:
        lines.append(f"📏 Driver → tujuan: ±{fmt_dist(dist)} garis lurus")
        init = t.get("initial_dist_km")
        if init and init > 0.05 and state in ("ORDER_EXECUTING", "PICKING_UP"):
            lines.append(f"🗺️ Perjalanan: {progress_bar(1 - dist / init)}")
    eta = (d.get("route") or {}).get("ETA")
    if eta:
        lines.append(f"⏱ Perkiraan tiba: {fmt_eta(eta)}")
    lines.append(f"\n<i>dipantau {mins} menit · cek ke-{t['checks']}, tiap {POLL_INTERVAL} dtk</i>")
    return "\n".join(lines)


def completion_text(d, t=None):
    drv = d.get("driver") or {}
    bk = d.get("booking") or {}
    pk = bk.get("pickup") or {}
    do = bk.get("dropOff") or {}
    dloc = drv.get("location") or {}
    doloc = do.get("location") or {}
    dist = None
    if dloc.get("latitude") and doloc.get("latitude"):
        dist = haversine_km(dloc["latitude"], dloc["longitude"],
                            doloc["latitude"], doloc["longitude"])
    s = ["✅ <b>Selesai</b>", "Pesanan tiba. Selamat menikmati! 🎉", "",
         f"Dibawa {esc(drv.get('name'))} (⭐{drv.get('rating') or '-'}) "
         f"pakai {esc(drv.get('vehicleModel'))} plat <code>{esc(drv.get('vehiclePlateNumber'))}</code>.",
         f"Diambil dari {esc(pk.get('keywords'))}, diantar ke {esc(do.get('keywords'))}"]
    if dist is not None:
        s[-1] += f" – {dist:.1f} km garis lurus."
    if t:
        now = time.time()
        dur = (now - t.get("started", now)) / 60
        s.append(f"\n⏱ Total waktu sejak dipantau: {dur:.0f} menit.")
        diff = (now - t["eta_first"]) / 60 if t.get("eta_first") else None
        if diff is not None:
            if diff <= -1:
                s.append(f"⚡ Tiba {-diff:.0f} menit <b>lebih cepat</b> dari estimasi awal 🎉")
            elif diff >= 1:
                s.append(f"🐌 Telat {diff:.0f} menit dari estimasi awal.")
            else:
                s.append("🎯 Tiba pas sesuai estimasi awal.")
    return "\n".join(s)


def milestone_text(frac, dist_km, eta_ts):
    """Pesan notifikasi saat progres perjalanan melewati milestone."""
    pct = int(frac * 100)
    head = {25: "🗺️ Perjalanan 25%!",
            50: "🗺️ Setengah jalan (50%)!",
            80: "🗺️ Hampir sampai (80%)!"}.get(pct, f"🗺️ Perjalanan {pct}%!")
    s = [head, f"Tinggal ±{fmt_dist(dist_km)} lagi ke tujuan."]
    if eta_ts:
        mins = (eta_ts - time.time()) / 60
        if mins > 0.5:
            s.append(f"⏱ Perkiraan tiba ±{mins:.0f} menit lagi.")
    if pct >= 80:
        s.append("Siap-siap ya! 🍜")
    return "\n".join(s)


def expired_text(t):
    """Ringkasan saat link kedaluwarsa — kemungkinan pesanan sudah sampai."""
    s = ["⌛ <b>Link pantauan kedaluwarsa</b>",
         "Kemungkinan pesanan sudah sampai — tercatat di riwayat. 🎉", "",
         f"🍜 {esc(t.get('merchant', '-'))} → {esc(t.get('dropoff', '-'))}",
         f"🛵 Driver: {esc(t.get('driver', '-'))}"]
    now = time.time()
    dur = (now - t.get("started", now)) / 60
    s.append(f"\n⏱ Total waktu sejak dipantau: {dur:.0f} menit.")
    diff = (now - t["eta_first"]) / 60 if t.get("eta_first") else None
    if diff is not None:
        if diff <= -1:
            s.append(f"⚡ Tiba {-diff:.0f} menit <b>lebih cepat</b> dari estimasi awal 🎉")
        elif diff >= 1:
            s.append(f"🐌 Telat {diff:.0f} menit dari estimasi awal.")
        else:
            s.append("🎯 Tiba pas sesuai estimasi awal.")
    return "\n".join(s)


def shopee_card_text(d, t):
    """Kartu status untuk pantauan ShopeeFood."""
    order = sp.order_of(d)
    store = order.get("store") or {}
    st = sp.status_code(order)
    mins = int((time.time() - t["started"]) // 60)
    lines = [
        f"🟧 <b>ShopeeFood — {esc(store.get('name') or t.get('merchant'))}</b>",
        "",
        f"📊 Status: {esc(sp.status_label(st))}",
    ]
    lo, hi = sp.eta_range(order)
    if lo:
        eta_s = fmt_eta(lo) + (f"–{fmt_eta(hi)}" if hi and hi != lo else "")
        lines.append(f"⏱ Estimasi tiba: {eta_s}")
    names = sp.item_names(order)
    n_items = len(order.get("items") or [])
    if names:
        more = f" +{n_items - len(names)} lainnya" if n_items > len(names) else ""
        lines.append(f"🍜 {esc(', '.join(names))}{more}")
    drv = sp.driver_of(d)
    dname = sp.driver_name(drv)
    if dname:
        rating = sp.driver_rating(drv)
        plate = sp.driver_plate(drv)
        veh = sp.driver_vehicle(drv)
        phone = sp.driver_phone(drv)
        lines += ["",
                  f"👤 {esc(dname)}" + (f" (⭐{rating})" if rating else ""),
                  f"🏍 {esc(veh or '-')} · <code>{esc(plate or '-')}</code>"]
        if phone:
            wa = re.sub(r"\D", "", phone)
            if wa.startswith("0"):
                wa = "62" + wa[1:]
            lines.append(f'📞 <a href="https://wa.me/{wa}">{esc(phone)}</a> '
                         f'<i>(ketuk untuk chat WA)</i>')
    dloc = sp.driver_loc(drv) or (sp.pickup_geo(d) if dname else None)
    dest = sp.dest_loc(order)
    if dloc and dest:
        dist = haversine_km(dloc[0], dloc[1], dest[0], dest[1])
        lines.append(f"📏 Driver → tujuan: ±{fmt_dist(dist)} garis lurus")
        init = t.get("initial_dist_km")
        if init and init > 0.05 and st in sp.DELIVERY_STATES:
            lines.append(f"🗺️ Perjalanan: {progress_bar(1 - dist / init)}")
    lines.append(f"\n<i>dipantau {mins} menit · cek ke-{t['checks']}, tiap {POLL_INTERVAL} dtk</i>")
    return "\n".join(lines)


def shopee_completion_text(d, t=None):
    order = sp.order_of(d)
    store = order.get("store") or {}
    dname = sp.driver_name(sp.driver_of(d)) or "-"
    s = ["✅ <b>Selesai</b>", "Pesanan ShopeeFood tiba. Selamat menikmati! 🎉", "",
         f"Dibawa {esc(dname)}." if dname != "-" else "Pesanan tiba.",
         f"Dari {esc(store.get('name') or '-') }."]
    if t:
        now = time.time()
        dur = (now - t.get("started", now)) / 60
        s.append(f"\n⏱ Total waktu sejak dipantau: {dur:.0f} menit.")
        diff = (now - t["eta_first"]) / 60 if t.get("eta_first") else None
        if diff is not None:
            if diff <= -1:
                s.append(f"⚡ Tiba {-diff:.0f} menit <b>lebih cepat</b> dari estimasi awal 🎉")
            elif diff >= 1:
                s.append(f"🐌 Telat {diff:.0f} menit dari estimasi awal.")
            else:
                s.append("🎯 Tiba pas sesuai estimasi awal.")
    return "\n".join(s)


def shopee_expired_text(t):
    """Ringkasan saat link ShopeeFood kedaluwarsa — kemungkinan pesanan sudah sampai."""
    s = ["⌛ <b>Link pantauan kedaluwarsa</b>",
         "Kemungkinan pesanan ShopeeFood sudah sampai — tercatat di riwayat. 🎉", "",
         f"🟧 {esc(t.get('merchant', '-'))} → {esc(t.get('dropoff', '-'))}",
         f"🛵 Driver: {esc(t.get('driver', '-'))}"]
    now = time.time()
    dur = (now - t.get("started", now)) / 60
    s.append(f"\n⏱ Total waktu sejak dipantau: {dur:.0f} menit.")
    diff = (now - t["eta_first"]) / 60 if t.get("eta_first") else None
    if diff is not None:
        if diff <= -1:
            s.append(f"⚡ Tiba {-diff:.0f} menit <b>lebih cepat</b> dari estimasi awal 🎉")
        elif diff >= 1:
            s.append(f"🐌 Telat {diff:.0f} menit dari estimasi awal.")
        else:
            s.append("🎯 Tiba pas sesuai estimasi awal.")
    return "\n".join(s)


def cleanup_tracking(s, tid, t, reason):
    try:
        if t.get("live_msg"):
            tg.stop_live_location(t["chat_id"], t["live_msg"])
    except Exception:
        pass
    s["trackings"].pop(str(tid), None)
    save_state(s)
    log(f"tracking {tid} stopped: {reason}")


def start_tracking(s, chat_id, link):
    """Dispatcher: Grab atau ShopeeFood tergantung linknya."""
    if "shopeefood.co.id/tracker" in (link or ""):
        start_shopee_tracking(s, chat_id, link)
    else:
        start_grab_tracking(s, chat_id, link)


def start_grab_tracking(s, chat_id, link):
    active = s["trackings"]
    if len(active) >= MAX_TRACKINGS:
        tg.send_message(chat_id,
                        f"⚠️ Maksimal {MAX_TRACKINGS} pesanan dipantau sekaligus. "
                        "Hentikan salah satu dulu lewat 📋 Daftar pantauan.",
                        reply_markup=tg.reply_keyboard())
        return
    token = resolve_token(link)
    if not token:
        log(f"link resolve FAILED: {link[:70]}")
        tg.send_message(chat_id,
                        "❌ Link tidak dikenali. Tempel link share dari aplikasi Grab, "
                        "contoh:\n<code>https://app.grab.com/s/xxxxxx</code>",
                        reply_markup=tg.reply_keyboard())
        return
    for tid, t in active.items():
        if t.get("token") == token:
            tg.send_message(chat_id, "ℹ️ Link ini sedang dipantau.",
                            reply_markup=tg.reply_keyboard())
            return
    d, status = fetch_details(token)
    if status == "error":
        log(f"link fetch ERROR (transient): token={token[:10]}...")
        tg.send_message(chat_id,
                        "⚠️ Gagal menghubungi Grab. Coba tempel ulang linknya.",
                        reply_markup=tg.reply_keyboard())
        return
    if status == "dead":
        log(f"link fetch DEAD: token={token[:10]}... "
            f"got_data={bool(d)} session={(d or {}).get('sessionStatus')}")
        tg.send_message(chat_id,
                        "❌ Link tidak valid atau sesi pantauannya sudah berakhir.",
                        reply_markup=tg.reply_keyboard())
        return
    log(f"link OK: token={token[:10]}... state={(d.get('booking') or {}).get('bookingState')}")
    bk = d.get("booking") or {}
    if bk.get("bookingState") == "COMPLETED":
        pk = bk.get("pickup") or {}
        do = bk.get("dropOff") or {}
        drv = d.get("driver") or {}
        tt = {"merchant": pk.get("keywords") or "-",
              "dropoff": do.get("keywords") or "-",
              "driver": drv.get("name") or "-",
              "started": time.time(),
              "eta_first": (d.get("route") or {}).get("ETA")}
        record_history(tt, d)
        log(f"history recorded (already completed at paste): {tt['merchant']}")
        tg.send_message(chat_id, completion_text(d),
                        reply_markup=tg.reply_keyboard())
        return
    tid = str(s["next_id"])
    s["next_id"] += 1
    drv = d.get("driver") or {}
    pk = bk.get("pickup") or {}
    do = bk.get("dropOff") or {}
    dloc0 = drv.get("location") or {}
    doloc0 = do.get("location") or {}
    dist0 = None
    if dloc0.get("latitude") and doloc0.get("latitude"):
        dist0 = haversine_km(dloc0["latitude"], dloc0["longitude"],
                             doloc0["latitude"], doloc0["longitude"])
    t = {"platform": "grab", "token": token, "chat_id": chat_id, "started": time.time(),
         "checks": 0, "fetch_fails": 0, "milestones": [],
         "last_state": bk.get("bookingState", ""),
         "notified_onway": False, "notified_near": False,
         "notified_stuck": False,
         "merchant": pk.get("keywords") or "-", "dropoff": do.get("keywords") or "-",
         "driver": drv.get("name") or "-", "card_msg": None, "live_msg": None,
         "eta_first": (d.get("route") or {}).get("ETA"),
         "initial_dist_km": dist0,
         "last_driver_pos": None, "last_move_ts": None}
    s["trackings"][tid] = t
    save_state(s)
    kb = tg.inline_stop(tid, token)
    r1 = tg.send_message(chat_id, card_text(d, t), reply_markup=kb)
    if r1.get("ok"):
        t["card_msg"] = r1["result"]["message_id"]
    dloc = drv.get("location") or {}
    if dloc.get("latitude"):
        r2 = tg.send_location(chat_id, dloc["latitude"], dloc["longitude"])
        if r2.get("ok"):
            t["live_msg"] = r2["result"]["message_id"]
    save_state(s)
    tg.send_message(chat_id,
                    f"👀 Mulai memantau <b>{esc(t['merchant'])}</b>. "
                    "Kartu di atas memperbarui diri sendiri.",
                    reply_markup=tg.reply_keyboard())
    log(f"tracking {tid} started: {t['merchant']} -> {t['dropoff']}")


def start_shopee_tracking(s, chat_id, link):
    active = s["trackings"]
    if len(active) >= MAX_TRACKINGS:
        tg.send_message(chat_id,
                        f"⚠️ Maksimal {MAX_TRACKINGS} pesanan dipantau sekaligus. "
                        "Hentikan salah satu dulu lewat 📋 Daftar pantauan.",
                        reply_markup=tg.reply_keyboard())
        return
    parsed = sp.parse_link(link)
    if not parsed:
        log(f"shopee link parse FAILED: {link[:70]}")
        tg.send_message(chat_id,
                        "❌ Link ShopeeFood tidak dikenali. Tempel link tracker dari "
                        "aplikasi ShopeeFood, contoh:\n"
                        "<code>https://www.shopeefood.co.id/tracker?code=...&amp;orderId=...</code>",
                        reply_markup=tg.reply_keyboard())
        return
    order_id, code = parsed
    for tid, t in active.items():
        if t.get("platform") == "shopee" and t.get("order_id") == order_id:
            tg.send_message(chat_id, "ℹ️ Link ini sedang dipantau.",
                            reply_markup=tg.reply_keyboard())
            return
    d, status = sp.fetch_details(order_id, code)
    if status == "error":
        log(f"shopee link fetch ERROR (transient): order={order_id}")
        tg.send_message(chat_id,
                        "⚠️ Gagal menghubungi ShopeeFood. Coba tempel ulang linknya.",
                        reply_markup=tg.reply_keyboard())
        return
    if status == "dead":
        log(f"shopee link fetch DEAD: order={order_id}")
        tg.send_message(chat_id,
                        "❌ Link tidak valid atau sesi pantauannya sudah berakhir.",
                        reply_markup=tg.reply_keyboard())
        return
    order = sp.order_of(d)
    st = sp.status_code(order)
    store = order.get("store") or {}
    dest = order.get("delivery_address") or {}
    merchant = store.get("name") or "-"
    dropoff = ((dest.get("location") or {}).get("address") or "-")[:80]
    lo, hi = sp.eta_range(order)
    log(f"shopee link OK: order={order_id} status={st} store={merchant}")
    tt = {"platform": "shopee", "merchant": merchant, "dropoff": dropoff,
          "driver": sp.driver_name(sp.driver_of(d)) or "-",
          "started": time.time(), "eta_first": lo}
    if sp.is_done(order):
        record_history(tt, d)
        log(f"history recorded (shopee already done at paste): {merchant}")
        tg.send_message(chat_id, shopee_completion_text(d),
                        reply_markup=tg.reply_keyboard())
        return
    if sp.is_cancelled(order):
        tg.send_message(chat_id, "🚫 Pesanan ShopeeFood dibatalkan.",
                        reply_markup=tg.reply_keyboard())
        return
    tid = str(s["next_id"])
    s["next_id"] += 1
    t = {"platform": "shopee", "order_id": order_id, "code": code,
         "tracker_url": sp.tracker_url(order_id, code),
         "chat_id": chat_id, "started": time.time(),
         "checks": 0, "fetch_fails": 0, "milestones": [],
         "last_state": st,
         "notified_assigned": False, "notified_picked": False,
         "notified_enroute": False, "notified_near": False,
         "notified_stuck": False,
         "merchant": merchant, "dropoff": dropoff,
         "driver": tt["driver"], "card_msg": None, "live_msg": None,
         "eta_first": lo, "initial_dist_km": None,
         "last_driver_pos": None, "last_move_ts": None}
    s["trackings"][tid] = t
    save_state(s)
    kb = tg.inline_stop_url(tid, t["tracker_url"], "📍 Buka di ShopeeFood")
    r1 = tg.send_message(chat_id, shopee_card_text(d, t), reply_markup=kb)
    if r1.get("ok"):
        t["card_msg"] = r1["result"]["message_id"]
    _drv0 = sp.driver_of(d)
    dloc = sp.driver_loc(_drv0) or (sp.pickup_geo(d) if sp.driver_name(_drv0) else None)
    if dloc:
        r2 = tg.send_location(chat_id, dloc[0], dloc[1])
        if r2.get("ok"):
            t["live_msg"] = r2["result"]["message_id"]
    save_state(s)
    tg.send_message(chat_id,
                    f"👀 Mulai memantau <b>{esc(merchant)}</b> (ShopeeFood). "
                    "Kartu di atas memperbarui diri sendiri.",
                    reply_markup=tg.reply_keyboard())
    log(f"tracking {tid} started (shopee): {merchant} -> {dropoff[:40]}")


def poll_shopee_tracking(s, tid, t):
    d, status = sp.fetch_details(t["order_id"], t["code"])
    t["checks"] += 1
    chat_id = t["chat_id"]
    if status == "error":
        # Gangguan sesaat — jangan langsung akhiri, coba lagi dulu
        t["fetch_fails"] = t.get("fetch_fails", 0) + 1
        save_state(s)
        if t["fetch_fails"] >= MAX_FETCH_FAILS:
            log(f"tracking {tid}: {MAX_FETCH_FAILS}x fetch error, giving up")
            tg.send_message(chat_id,
                            "⚠️ Koneksi ke ShopeeFood bermasalah berulang kali, "
                            "pantauan dihentikan.\nRiwayat tidak tercatat karena "
                            "status pesanan tidak diketahui — tempel ulang linknya "
                            "kalau masih dibutuhkan.",
                            reply_markup=tg.reply_keyboard())
            cleanup_tracking(s, tid, t, "fetch errors")
        return
    t["fetch_fails"] = 0
    if status == "dead":
        # Link kedaluwarsa = pesanan kemungkinan besar sudah sampai
        entry = record_history(t, d, ended="expired")
        log(f"history recorded (shopee link expired): {entry['merchant']} "
            f"({entry['duration_min']} mnt)")
        if t.get("card_msg"):
            tg.edit_message(chat_id, t["card_msg"], shopee_expired_text(t))
        else:
            tg.send_message(chat_id, shopee_expired_text(t),
                            reply_markup=tg.reply_keyboard())
        s["reminders"].append({
            "chat_id": chat_id, "platform": "shopee",
            "merchant": t.get("merchant", "-"),
            "driver": t.get("driver", "-"),
            "at": time.time() + RATING_REMINDER_MINUTES * 60})
        save_state(s)
        cleanup_tracking(s, tid, t, "link expired")
        return
    order = sp.order_of(d)
    st = sp.status_code(order)
    drv = sp.driver_of(d)
    dname = sp.driver_name(drv)
    if dname:
        t["driver"] = dname
    # Sekali saja saat driver pertama muncul: catat field mentah driver
    # (untuk verifikasi apakah API share ShopeeFood menyertakan kontak driver)
    if dname and not t.get("driver_fields_logged"):
        t["driver_fields_logged"] = True
        keys = sorted(drv.keys())
        phone = sp.driver_phone(drv)
        masked = (phone[:4] + "***" + phone[-2:]) if phone else "-"
        log(f"tracking {tid}: shopee driver fields: {keys} "
            f"| phone_present={bool(phone)} phone_masked={masked}")

    if sp.is_done(order):
        entry = record_history(t, d)
        log(f"history recorded (shopee): {entry['merchant']} ({entry['duration_min']} mnt)")
        if t.get("card_msg"):
            tg.edit_message(chat_id, t["card_msg"], shopee_completion_text(d, t))
        else:
            tg.send_message(chat_id, shopee_completion_text(d, t),
                            reply_markup=tg.reply_keyboard())
        s["reminders"].append({
            "chat_id": chat_id, "platform": "shopee",
            "merchant": t.get("merchant", "-"),
            "driver": dname or t.get("driver", "-"),
            "at": time.time() + RATING_REMINDER_MINUTES * 60})
        save_state(s)
        cleanup_tracking(s, tid, t, "completed")
        return
    if sp.is_cancelled(order):
        tg.send_message(chat_id, "🚫 Pesanan ShopeeFood dibatalkan.",
                        reply_markup=tg.reply_keyboard())
        cleanup_tracking(s, tid, t, "cancelled")
        return
    if time.time() - t["started"] > MAX_TRACK_MINUTES * 60:
        tg.send_message(chat_id,
                        "⌛ Pantauan dihentikan otomatis (melebihi 2 jam).",
                        reply_markup=tg.reply_keyboard())
        cleanup_tracking(s, tid, t, "timeout")
        return

    # transisi status khas ShopeeFood
    if st == 400 and not t.get("notified_assigned"):
        t["notified_assigned"] = True
        tg.send_message(chat_id, "🔔 Driver ShopeeFood sudah ditugaskan. 🟧")
    if st == 430 and not t.get("notified_picked"):
        t["notified_picked"] = True
        tg.send_message(chat_id, "🔔 Pesananmu sudah diambil driver ShopeeFood.")
    if st == 431 and not t.get("notified_enroute"):
        t["notified_enroute"] = True
        tg.send_message(chat_id, "🔔 Driver ShopeeFood sedang menuju lokasimu. 🟧")

    dloc_real = sp.driver_loc(drv)
    dloc = dloc_real or (sp.pickup_geo(d) if dname else None)
    dest = sp.dest_loc(order)
    dist = None
    if dloc and dest:
        dist = haversine_km(dloc[0], dloc[1], dest[0], dest[1])
    # baseline jarak rute saat driver mulai membawa pesanan
    if t.get("initial_dist_km") is None and st in sp.DELIVERY_STATES:
        pk = sp.store_loc(order)
        if pk and dest:
            t["initial_dist_km"] = haversine_km(pk[0], pk[1], dest[0], dest[1])
    # driver dekat tujuan
    if (not t["notified_near"] and dist is not None
            and dist <= NEAR_THRESHOLD_KM and st in sp.DELIVERY_STATES):
        t["notified_near"] = True
        tg.send_message(
            chat_id,
            f"🟧 Driver ShopeeFood sudah dekat — tinggal sekitar {int(dist * 1000)} meter lagi.")
    # notifikasi milestone progres perjalanan (25% / 50% / 80%)
    init = t.get("initial_dist_km")
    if (dist is not None and init and init > 0.05
            and st in sp.DELIVERY_STATES):
        prog = max(0.0, min(1.0, 1 - dist / init))
        done = set(t.get("milestones") or [])
        crossed = [m for m in MILESTONES if prog >= m and m not in done]
        if crossed:
            top = max(crossed)
            t["milestones"] = sorted(m for m in MILESTONES if m <= top)
            _lo, hi = sp.eta_range(order)
            tg.send_message(chat_id, milestone_text(top, dist, hi))
            log(f"tracking {tid}: milestone {int(top * 100)}% "
                f"(sisa {fmt_dist(dist)})")
    # driver berhenti lama saat mengantar (hanya bila lokasi live asli ada,
    # bukan pin fallback resto — biar tidak false alarm saat baru pickup)
    if st in sp.DELIVERY_STATES and dloc_real:
        now2 = time.time()
        lp = t.get("last_driver_pos")
        moved = True
        if lp:
            moved = haversine_km(lp[0], lp[1], dloc[0], dloc[1]) >= STUCK_DIST_KM
        if moved:
            t["last_driver_pos"] = [dloc[0], dloc[1]]
            t["last_move_ts"] = now2
        elif (not t.get("notified_stuck")
                and now2 - (t.get("last_move_ts") or t["started"]) >= STUCK_MINUTES * 60):
            t["notified_stuck"] = True
            tg.send_message(chat_id,
                            f"🚦 {esc(t.get('driver', 'Driver'))} belum bergerak ~{STUCK_MINUTES} menit "
                            "— mungkin macet atau mampir sebentar.")
    t["last_state"] = st

    # update live location
    if dloc:
        if t.get("live_msg"):
            r = tg.edit_live_location(chat_id, t["live_msg"], dloc[0], dloc[1])
            if not r.get("ok") and "live location" in str(r.get("description", "")).lower():
                r2 = tg.send_location(chat_id, dloc[0], dloc[1])
                if r2.get("ok"):
                    t["live_msg"] = r2["result"]["message_id"]
        else:
            r2 = tg.send_location(chat_id, dloc[0], dloc[1])
            if r2.get("ok"):
                t["live_msg"] = r2["result"]["message_id"]
    # update kartu
    if t.get("card_msg"):
        tg.edit_message(chat_id, t["card_msg"], shopee_card_text(d, t),
                        reply_markup=tg.inline_stop_url(
                            tid, t["tracker_url"], "📍 Buka di ShopeeFood"))
    save_state(s)


def poll_tracking(s, tid, t):
    d, status = fetch_details(t["token"])
    t["checks"] += 1
    chat_id = t["chat_id"]
    if status == "error":
        # Gangguan sesaat — jangan langsung akhiri, coba lagi dulu
        t["fetch_fails"] = t.get("fetch_fails", 0) + 1
        save_state(s)
        if t["fetch_fails"] >= MAX_FETCH_FAILS:
            log(f"tracking {tid}: {MAX_FETCH_FAILS}x fetch error, giving up")
            tg.send_message(chat_id,
                            "⚠️ Koneksi ke Grab bermasalah berulang kali, "
                            "pantauan dihentikan.\nRiwayat tidak tercatat karena "
                            "status pesanan tidak diketahui — tempel ulang linknya "
                            "kalau masih dibutuhkan.",
                            reply_markup=tg.reply_keyboard())
            cleanup_tracking(s, tid, t, "fetch errors")
        return
    t["fetch_fails"] = 0
    if status == "dead":
        # Link kedaluwarsa = pesanan kemungkinan besar sudah sampai
        entry = record_history(t, d, ended="expired")
        log(f"history recorded (link expired): {entry['merchant']} "
            f"({entry['duration_min']} mnt)")
        if t.get("card_msg"):
            tg.edit_message(chat_id, t["card_msg"], expired_text(t))
        else:
            tg.send_message(chat_id, expired_text(t),
                            reply_markup=tg.reply_keyboard())
        s["reminders"].append({
            "chat_id": chat_id, "platform": "grab",
            "merchant": t.get("merchant", "-"),
            "driver": t.get("driver", "-"),
            "at": time.time() + RATING_REMINDER_MINUTES * 60})
        save_state(s)
        cleanup_tracking(s, tid, t, "link expired")
        return
    bk = d.get("booking") or {}
    state = bk.get("bookingState", "")
    drv = d.get("driver") or {}
    do = bk.get("dropOff") or {}
    dloc = drv.get("location") or {}
    doloc = do.get("location") or {}

    if state == "COMPLETED":
        entry = record_history(t, d)
        log(f"history recorded: {entry['merchant']} ({entry['duration_min']} mnt)")
        if t.get("card_msg"):
            tg.edit_message(chat_id, t["card_msg"], completion_text(d, t))
        else:
            tg.send_message(chat_id, completion_text(d, t),
                            reply_markup=tg.reply_keyboard())
        s["reminders"].append({
            "chat_id": chat_id, "platform": "grab",
            "merchant": t.get("merchant", "-"),
            "driver": (d.get("driver") or {}).get("name") or t.get("driver", "-"),
            "at": time.time() + RATING_REMINDER_MINUTES * 60})
        save_state(s)
        cleanup_tracking(s, tid, t, "completed")
        return
    if state in CANCELLED:
        tg.send_message(chat_id, "🚫 Pesanan dibatalkan.",
                        reply_markup=tg.reply_keyboard())
        cleanup_tracking(s, tid, t, f"cancelled {state}")
        return
    if time.time() - t["started"] > MAX_TRACK_MINUTES * 60:
        tg.send_message(chat_id,
                        "⌛ Pantauan dihentikan otomatis (melebihi 2 jam).",
                        reply_markup=tg.reply_keyboard())
        cleanup_tracking(s, tid, t, "timeout")
        return

    # transisi: driver jalan membawa pesanan
    if (not t["notified_onway"] and t["last_state"] in ("ORDER_IN_PREPARE", "")
            and state in ("ORDER_EXECUTING", "PICKING_UP")):
        t["notified_onway"] = True
        tg.send_message(chat_id, "🔔 Driver sudah jalan membawa pesananmu.")
    # driver dekat tujuan
    dist = None
    if dloc.get("latitude") and doloc.get("latitude"):
        dist = haversine_km(dloc["latitude"], dloc["longitude"],
                            doloc["latitude"], doloc["longitude"])
    if (not t["notified_near"] and dist is not None and dist <= NEAR_THRESHOLD_KM):
        t["notified_near"] = True
        tg.send_message(
            chat_id,
            f"🛵 Driver sudah dekat — tinggal sekitar {int(dist * 1000)} meter lagi ke tujuan.")
    # isi jarak awal untuk progress bar bila belum ada
    if t.get("initial_dist_km") is None and dist:
        t["initial_dist_km"] = dist
    # notifikasi milestone progres perjalanan (25% / 50% / 80%)
    init = t.get("initial_dist_km")
    if (dist is not None and init and init > 0.05
            and state in ("ORDER_EXECUTING", "PICKING_UP")):
        prog = max(0.0, min(1.0, 1 - dist / init))
        done = set(t.get("milestones") or [])
        crossed = [m for m in MILESTONES if prog >= m and m not in done]
        if crossed:
            top = max(crossed)
            # tandai semua milestone di bawahnya juga (biar tidak spam
            # kalau progres melonjak sekaligus)
            t["milestones"] = sorted(m for m in MILESTONES if m <= top)
            eta_ts = (d.get("route") or {}).get("ETA")
            tg.send_message(chat_id, milestone_text(top, dist, eta_ts))
            log(f"tracking {tid}: milestone {int(top * 100)}% "
                f"(sisa {fmt_dist(dist)})")
    # driver berhenti lama saat mengantar
    if state == "ORDER_EXECUTING" and dloc.get("latitude"):
        now2 = time.time()
        lp = t.get("last_driver_pos")
        moved = True
        if lp:
            moved = haversine_km(lp[0], lp[1],
                                 dloc["latitude"], dloc["longitude"]) >= STUCK_DIST_KM
        if moved:
            t["last_driver_pos"] = [dloc["latitude"], dloc["longitude"]]
            t["last_move_ts"] = now2
        elif (not t.get("notified_stuck")
                and now2 - (t.get("last_move_ts") or t["started"]) >= STUCK_MINUTES * 60):
            t["notified_stuck"] = True
            tg.send_message(chat_id,
                            f"🚦 {esc(t.get('driver', 'Driver'))} belum bergerak ~{STUCK_MINUTES} menit "
                            "— mungkin macet atau mampir sebentar.")
    t["last_state"] = state

    # update live location
    if dloc.get("latitude"):
        if t.get("live_msg"):
            r = tg.edit_live_location(chat_id, t["live_msg"],
                                      dloc["latitude"], dloc["longitude"])
            if not r.get("ok") and "live location" in str(r.get("description", "")).lower():
                r2 = tg.send_location(chat_id, dloc["latitude"], dloc["longitude"])
                if r2.get("ok"):
                    t["live_msg"] = r2["result"]["message_id"]
        else:
            r2 = tg.send_location(chat_id, dloc["latitude"], dloc["longitude"])
            if r2.get("ok"):
                t["live_msg"] = r2["result"]["message_id"]
    # update kartu
    if t.get("card_msg"):
        tg.edit_message(chat_id, t["card_msg"], card_text(d, t),
                        reply_markup=tg.inline_stop(tid, t["token"]))
    save_state(s)


def list_trackings(s, chat_id):
    active = s["trackings"]
    if not active:
        tg.send_message(chat_id, "📋 Tidak ada pesanan yang sedang dipantau.",
                        reply_markup=tg.reply_keyboard())
        return
    lines = ["📋 <b>Sedang dipantau ({})</b>".format(len(active))]
    kb_rows = []
    for i, (tid, t) in enumerate(active.items(), 1):
        mins = int((time.time() - t["started"]) // 60)
        pf = "🟧" if t.get("platform") == "shopee" else "🛵"
        if t.get("platform") == "shopee":
            label = sp.status_label(t.get("last_state"))
        else:
            label = STATE_LABEL.get(t.get("last_state"), "")
        lines.append(f"{pf} {esc(t['merchant'])} → {esc(t['dropoff'])}")
        lines.append(f"   <i>{esc(label)} · {mins} mnt · cek ke-{t['checks']}</i>")
        kb_rows.append([{"text": f"⏹ Stop #{i}", "callback_data": f"stop:{tid}"}])
    tg.send_message(chat_id, "\n".join(lines),
                    reply_markup={"inline_keyboard": kb_rows})


def stats_body_and_kb(s):
    h = load_history()
    on = s.get("settings", {}).get("rating_reminder", True)
    kb = {"inline_keyboard": [[{
        "text": f"🔔 Pengingat rating: {'ON' if on else 'OFF'}",
        "callback_data": "toggle_reminder"}]]}
    if not h:
        return ("📊 <b>Statistik</b>\n\nBelum ada pesanan yang selesai dipantau. "
                "Riwayat tercatat otomatis setiap pesanan selesai.", kb)
    total = len(h)
    top = Counter(x["merchant"] for x in h).most_common(3)
    avg_dur = sum(x["duration_min"] for x in h) / total
    ratings = [x["rating"] for x in h if x.get("rating")]
    lines = ["📊 <b>Statistik pantauan</b>", "",
             f"🍜 {total} pesanan selesai dipantau",
             f"⏱ Rata-rata pesan → tiba: {avg_dur:.0f} menit"]
    if ratings:
        lines.append(f"⭐ Rata-rata rating driver: {sum(ratings) / len(ratings):.1f}")
    lines += ["", "<b>Restoran favorit:</b>"]
    for name, c in top:
        lines.append(f"• {esc(name)} ({c}x)")
    lines += ["", "<b>Terakhir:</b>"]
    for x in h[-5:][::-1]:
        dt = datetime.fromtimestamp(x["completed_ts"], WIB).strftime("%d/%m %H:%M")
        pf = "🟧" if x.get("platform") == "shopeefood" else "🛵"
        lines.append(f"{pf} {esc(x['merchant'])} — {dt}")
    return ("\n".join(lines), kb)


def show_stats(s, chat_id):
    body, kb = stats_body_and_kb(s)
    tg.send_message(chat_id, body, reply_markup=kb)


WELCOME = """🛵🍊 <b>Pelacak pesanan Grab & ShopeeFood</b>

Tempel link share-nya di sini — langsung dipantau, tidak perlu mengetik perintah apa pun.
<code>app.grab.com/s/...</code> atau <code>shopeefood.co.id/tracker?...</code>

Kartunya memperbarui diri sendiri sampai pesanan selesai. Mau berhenti lebih cepat? Tekan ⏹ Stop di bawah kartu.

Menu di bawah kolom tulis:
🛵 Lacak pesanan — mulai, tinggal tempel link
📋 Daftar pantauan — hentikan yang sedang dipantau
📊 Statistik — riwayat pesanan, restoran favorit & rata-rata waktumu

Ekstra: progress bar perjalanan di kartu, info tiba cepat/telat vs estimasi,
pengingat kasih rating ⭐, dan notifikasi kalau driver berhenti lama 🚦.

<i>maksimal 3 pesanan sekaligus · menunya hilang? kirim /start</i>"""


def handle_message(s, m):
    chat = m.get("chat") or {}
    chat_id = chat.get("id")
    frm = m.get("from") or {}
    text = (m.get("text") or "").strip()
    if not chat_id:
        return
    if s["owner_id"] is None:
        s["owner_id"] = chat_id
        save_state(s)
        log(f"owner set: {chat_id} ({frm.get('first_name')})")
    if chat_id != s["owner_id"]:
        tg.send_message(chat_id, "🔒 Bot ini privat dan hanya untuk pemiliknya.")
        return
    if text == "/start":
        s["awaiting_link"] = False
        save_state(s)
        tg.send_message(chat_id, WELCOME, reply_markup=tg.reply_keyboard())
        return
    if text == "🛵 Lacak pesanan":
        s["awaiting_link"] = True
        save_state(s)
        tg.send_message(chat_id, "Tempel link Grab / ShopeeFood-nya di sini 👇",
                        reply_markup=tg.reply_keyboard())
        return
    if text == "📋 Daftar pantauan":
        list_trackings(s, chat_id)
        return
    if text == "📊 Statistik":
        show_stats(s, chat_id)
        return
    if re.search(r"(sharelocation\.grab\.com/o/|app\.grab\.com/s/|shopeefood\.co\.id/tracker)", text):
        s["awaiting_link"] = False
        m_link = re.search(r"https?://\S*", text)
        link = m_link.group(0) if m_link else text
        log(f"link received from {chat_id}: {link[:70]}")
        start_tracking(s, chat_id, link)
        save_state(s)
        return
    if s.get("awaiting_link"):
        s["awaiting_link"] = False
        save_state(s)
        tg.send_message(chat_id,
                        "❌ Itu bukan link Grab/ShopeeFood yang dikenali. Coba tempel lagi ya.",
                        reply_markup=tg.reply_keyboard())
        return
    # teks tak dikenal: diam saja (bot privat satu pengguna)
    return


def handle_callback(s, cb):
    data = cb.get("data") or ""
    chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
    if chat_id != s["owner_id"]:
        tg.answer_callback(cb["id"], "Bot privat.")
        return
    if data == "toggle_reminder":
        cur = s["settings"].get("rating_reminder", True)
        s["settings"]["rating_reminder"] = not cur
        save_state(s)
        tg.answer_callback(cb["id"],
                           "Pengingat rating " + ("aktif ✅" if not cur else "nonaktif"))
        body, kb = stats_body_and_kb(s)
        msg = cb.get("message") or {}
        if msg.get("message_id"):
            tg.edit_message(chat_id, msg["message_id"], body, reply_markup=kb)
        return
    m = re.match(r"stop:(\d+)", data)
    if not m:
        tg.answer_callback(cb["id"])
        return
    tid = m.group(1)
    t = s["trackings"].get(tid)
    if not t:
        tg.answer_callback(cb["id"], "Sudah berhenti.")
        return
    if t.get("card_msg"):
        try:
            tg.edit_message(chat_id, t["card_msg"],
                            f"⏹ Pantauan dihentikan.\n🍜 {esc(t['merchant'])} → {esc(t['dropoff'])}")
        except Exception:
            pass
    cleanup_tracking(s, tid, t, "stopped by user")
    tg.answer_callback(cb["id"], "Pantauan dihentikan.")
    tg.send_message(chat_id, "⏹ Pantauan dihentikan.",
                    reply_markup=tg.reply_keyboard())


def main():
    s = load_state()
    log("bot started")
    # resume: pastikan pesan kartu/live masih valid tidak dicek; cukup lanjutkan polling
    if s["trackings"]:
        log(f"resuming {len(s['trackings'])} trackings")
    last_tick = 0
    while True:
        try:
            out = tg.api("getUpdates", {"timeout": 25,
                                        "offset": s["offset"] or 0,
                                        "allowed_updates": ["message", "callback_query"]},
                         timeout=40)
        except Exception as e:
            log(f"getUpdates error: {e}")
            time.sleep(5)
            continue
        if not out.get("ok"):
            log(f"getUpdates not ok: {out}")
            time.sleep(5)
            continue
        for u in out.get("result", []):
            s["offset"] = u["update_id"] + 1
            try:
                if u.get("message"):
                    handle_message(s, u["message"])
                elif u.get("callback_query"):
                    handle_callback(s, u["callback_query"])
            except Exception as e:
                log(f"handler error: {e}")
        save_state(s)
        # pengingat rating — jalan walau tidak ada pantauan aktif
        now = time.time()
        for r in list(s.get("reminders", [])):
            if now >= r["at"]:
                s["reminders"].remove(r)
                if s["settings"].get("rating_reminder", True):
                    app = "ShopeeFood" if r.get("platform") == "shopee" else "Grab"
                    tg.send_message(
                        r["chat_id"],
                        f"⭐ Pesananmu dari <b>{esc(r['merchant'])}</b> sudah "
                        f"{RATING_REMINDER_MINUTES} menit tiba. Jangan lupa kasih bintang "
                        f"buat {esc(r['driver'])} di aplikasi {app} ya! 🙏",
                        reply_markup=tg.reply_keyboard())
                save_state(s)
        now = time.time()
        if now - last_tick >= POLL_INTERVAL and s["trackings"]:
            last_tick = now
            for tid in list(s["trackings"].keys()):
                t = s["trackings"].get(tid)
                if not t:
                    continue
                try:
                    if t.get("platform") == "shopee":
                        poll_shopee_tracking(s, tid, t)
                    else:
                        poll_tracking(s, tid, t)
                except Exception as e:
                    log(f"poll error {tid}: {e}")
                time.sleep(1)


if __name__ == "__main__":
    main()
