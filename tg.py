"""Telegram Bot API wrapper.

Token resolution order:
  1. TELEGRAM_BOT_TOKEN env var (raw bot token, for running anywhere).
  2. The stored custom.telegram credential (Muse environment): the returned
     value is a surrogate the egress proxy replaces with the real token.

NOTE: the bundled url_with_surrogate_path_segment() percent-encodes the
surrogate (hsurr%3A...) which the egress proxy does not recognise, so the
literal surrogate reaches Telegram and every call 404s. We insert the
surrogate unencoded instead (it is only `hsurr:` + hex, path-safe).
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

HOST = "api.telegram.org"
_surrogate_val = None
_surrogate_ts = 0


def _get_surrogate():
    global _surrogate_val, _surrogate_ts
    env_tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if env_tok:
        return env_tok
    if _surrogate_val and time.time() - _surrogate_ts < 600:
        return _surrogate_val
    # Muse environment: token dari kredensial custom.telegram (lazy import
    # agar modul ini tetap bisa dipakai di luar lingkungan Muse)
    sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
    from dynamic_credentials import dynamic_credential_entry
    _surrogate_val = dynamic_credential_entry("custom.telegram")["surrogate"].strip()
    _surrogate_ts = time.time()
    return _surrogate_val


def api(method, payload=None, timeout=30, _retry=True):
    global _surrogate_val
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    url = f"https://{HOST}/bot{_get_surrogate()}/{method}"
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401 and _retry:
            _surrogate_val = None
            return api(method, payload, timeout, _retry=False)
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            body = ""
        return {"ok": False, "error_code": e.code, "description": body}
    except Exception as e:
        return {"ok": False, "error_code": -1, "description": str(e)[:200]}


def send_message(chat_id, text, reply_markup=None, parse_mode="HTML",
                 disable_web_page_preview=True, entities=None):
    p = {"chat_id": chat_id, "text": text,
         "disable_web_page_preview": disable_web_page_preview}
    if entities is not None:
        # entities & parse_mode tidak bisa dipakai bersamaan — entities menang
        p["entities"] = entities
    else:
        p["parse_mode"] = parse_mode
    if reply_markup:
        p["reply_markup"] = reply_markup
    return api("sendMessage", p, timeout=20)


def edit_message(chat_id, msg_id, text, reply_markup=None, parse_mode="HTML",
                 entities=None):
    p = {"chat_id": chat_id, "message_id": msg_id, "text": text,
         "disable_web_page_preview": True}
    if entities is not None:
        p["entities"] = entities
    else:
        p["parse_mode"] = parse_mode
    if reply_markup is not None:
        p["reply_markup"] = reply_markup
    return api("editMessageText", p, timeout=20)


def answer_callback(cb_id, text=""):
    return api("answerCallbackQuery", {"callback_query_id": cb_id, "text": text},
               timeout=15)


def send_location(chat_id, lat, lng, live_period=3600):
    return api("sendLocation", {"chat_id": chat_id, "latitude": lat,
                                "longitude": lng, "live_period": live_period},
               timeout=20)


def edit_live_location(chat_id, msg_id, lat, lng):
    return api("editMessageLiveLocation",
               {"chat_id": chat_id, "message_id": msg_id,
                "latitude": lat, "longitude": lng}, timeout=20)


def stop_live_location(chat_id, msg_id):
    return api("stopMessageLiveLocation",
               {"chat_id": chat_id, "message_id": msg_id}, timeout=20)


def reply_keyboard():
    return {"keyboard": [[{"text": "🛵 Lacak pesanan"}, {"text": "📋 Daftar pantauan"}],
                         [{"text": "📊 Statistik"}]],
            "resize_keyboard": True}


def inline_stop(tid, token):
    return {"inline_keyboard": [
        [{"text": "⏹ Stop", "callback_data": f"stop:{tid}"},
         {"text": "📍 Buka peta driver",
          "url": f"https://sharelocation.grab.com/o/{token}"}]]}


def inline_stop_url(tid, url, label="📍 Buka pelacakan"):
    """Varian tombol Stop + link untuk platform selain Grab."""
    return {"inline_keyboard": [
        [{"text": "⏹ Stop", "callback_data": f"stop:{tid}"},
         {"text": label, "url": url}]]}
