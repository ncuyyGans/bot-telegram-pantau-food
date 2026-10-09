# Pantau GrabFood & ShopeeFood — Bot Telegram Pribadi

Bot Telegram pribadi untuk memantau pesanan GrabFood **dan ShopeeFood** lewat
link share. Tempel link `https://app.grab.com/s/xxxxxx`,
`https://sharelocation.grab.com/o/xxxxxx`, atau
`https://www.shopeefood.co.id/tracker?code=...&orderId=...` — bot memantau
posisi driver tiap 10 detik lewat kartu status + live location, lengkap dengan
notifikasi.

## Menjalankan di tempat lain

- Python 3.10+, **tanpa dependensi eksternal** (stdlib saja).
- Beri token bot Telegram lewat environment variable, lalu jalankan:
  ```bash
  export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
  python3 bot.py
  ```
  Jangan pernah commit token ke repo! (Di lingkungan Muse, token diambil
  otomatis dari kredensial `custom.telegram` bila env var tidak diset.)
- Opsional — jaga proses tetap hidup, mis. cron tiap 5 menit:
  ```bash
  */5 * * * * bash /path/ke/repo/watchdog.sh
  ```
- `state.json` dan `history.json` dibuat otomatis saat pertama jalan;
  keduanya di-gitignore karena berisi data pribadi
  (lihat `state.example.json` untuk skemanya).

## Cara pakai (di Telegram)

1. Kirim `/start`
2. Tempel link share dari aplikasi Grab (`https://app.grab.com/s/xxxxxx`
   atau `https://sharelocation.grab.com/o/xxxxxx`) atau dari aplikasi
   ShopeeFood (`https://www.shopeefood.co.id/tracker?code=...&orderId=...`)
3. Bot mengirim:
   - **Kartu status** (memperbarui diri tiap 10 detik): status pesanan, nama
     driver + rating, kendaraan + plat, asal, tujuan, jarak driver→tujuan,
     🗺️ progress bar perjalanan, perkiraan tiba. Tombol: ⏹ Stop,
     📍 Buka peta driver (Grab) / 📍 Buka di ShopeeFood.
   - **Live location** mengikuti posisi driver.
4. Notifikasi otomatis:
   - 🔔 "Driver sudah jalan membawa pesananmu."
   - 🛵 "Driver sudah dekat — tinggal sekitar X meter lagi ke tujuan."
   - 🚦 "Driver belum bergerak ~6 menit — mungkin macet atau mampir sebentar."
     (saat mengantar)
   - ✅ "Pesanan tiba!" + ringkasan: total durasi sejak dipantau, tiba
     lebih cepat / telat X menit vs estimasi awal, driver, rating,
     kendaraan, plat, asal → tujuan, jarak garis lurus.
   - ⭐ 10 menit setelah tiba: pengingat kasih bintang ke driver
     (bisa ON/OFF dari menu 📊 Statistik).
   - 🗺️ Milestone perjalanan: pesan saat progres 25% / 50% / 80%,
     lengkap dengan sisa jarak & perkiraan menit tiba.
   - ⌛ Link kedaluwarsa = pesanan dianggap sampai: ringkasan dikirim,
     tercatat di riwayat (`ended: "expired"`), dan pengingat rating tetap
     dijadwalkan. Gangguan jaringan sesaat tidak menghentikan pantauan
     (baru berhenti setelah 12x gagal fetch ≈ 2 menit).
5. Menu: 🛵 Lacak pesanan · 📋 Daftar pantauan (maks 3 bersamaan) ·
   📊 Statistik (riwayat pesanan selesai: total, restoran favorit,
   rata-rata durasi & rating driver — tersimpan di `history.json`).

## Arsitektur

| File | Fungsi |
|---|---|
| `bot.py` | Long-polling getUpdates, handler pesan/callback, loop pantau tiap 10 dtk |
| `tg.py` | Wrapper Bot API via kredensial `custom.telegram` |
| `grab_client.py` | Klien API Grab: resolve shortlink → token, `GET api.grab.com/api/v1/safety/sharemyride/{token}/bookingdetails`. Catatan: `app.grab.com/s/…` kadang me-return HTTP 200 (halaman SPA) bukan 302 — `resolve_token` mengikuti redirect sampai URL final lalu baca `shareOrderLink=`, dengan retry 4x |
| `shopee_client.py` | Klien API ShopeeFood: parse `orderId`+`code` dari link tracker, `GET www.shopeefood.co.id/api/buyer/orders/{orderId}/tracing/{code}` (tanpa auth). Ditemukan dari bundle JS `main.*.chunk.js` halaman tracker |
| `state.json` | owner_id, offset getUpdates, daftar pantauan aktif (tahan restart), antrian pengingat rating, settings |
| `history.json` | Riwayat pesanan selesai (maks 100): merchant, driver, rating, durasi, selisih vs estimasi |
| `bot.pid` / `bot.log` | pid proses + log |
| `watchdog.sh` | Dipanggil cron tiap 5 mnt; start ulang bot bila mati |

## Catatan teknis

### API Grab
- API Grab (`sharemyride/.../bookingdetails`) tidak butuh auth; ditemukan dari
  bundle JS `sharelocation.grab.com` (`config.json` → `uri: https://api.grab.com`).

### API ShopeeFood (baru 2026-10-09)
- Endpoint: `GET https://www.shopeefood.co.id/api/buyer/orders/{orderId}/tracing/{code}`
  — tanpa auth, `code` disambung mentah ke path (boleh mengandung `=`).
  Ditemukan dari bundle JS halaman tracker (`main.*.chunk.js`):
  axios `baseURL: "/api/buyer"` + `bo.get("orders/".concat(orderId,"/tracing/").concat(code))`.
- Respons: `{"code":0,"msg":"success","data":{"order":{...},"payment":[...]}}`.
  Kode error: `1000` ParamInvalid, `2001` RecordNotFound,
  `11160051` ShareTokenInvalid, `11160052` LinkIsExpired → dianggap link mati.
- Status pesanan numerik (dilihat dari web app): `300` Confirmed
  (resto menyiapkan), `400` Assigned, `411` EnterProcess, `412` ToCollect,
  `425` Reconfirmed, `430` Picked, `431` EnrouteDelivery. Selesai/batal
  dideteksi lewat `complete_time` / `delivery_complete_time` / `cancel_time`
  yang terisi (nilai numerik Delivered/Completed/Cancelled tidak ditemukan
  di bundle JS).
- Nominal uang dalam satuan 1/100rb rupiah; waktu dalam ms epoch.
  Lokasi driver: `order.driver.location.{latitude,longitude}` (ada setelah
  driver ditugaskan). Info driver: `full_name`, `rating`, `vehicle_plate_no`,
  `vehicle_description`.
- Info driver TIDAK ada di `order.driver` saat diambil — pindah ke
  `data.union_delivery.driver` (sesuai fallback di web app:
  `ke.driver || e.unionDelivery?.driver`). Objeknya berisi `full_name`,
  **`phone`**, `vehicle_plate_no`, `profile_photo`, `rating_score` —
  nomor HP driver memang dikirim lewat link share ini (halaman web-nya
  sendiri tidak menampilkannya, tapi bot menampilkannya di kartu).
  Lokasi live driver: `driver.location` (muncul belakangan).
- Kode share **dirotasi** ShopeeFood (diamati 2026-10-09: suffix `=<timestamp>`
  berubah tiap ~30 menit / tiap buka aplikasi; kode lama tetap menjawab tapi
  datanya bisa basi). Bot menanganinya: tempel link baru untuk orderId yang
  sedang dipantau → kode pantauan di-update in-place (bukan duplikat);
  bila kode mati total, pantauan dijeda 30 menit menunggu link baru
  (`LINK_DEAD_GRACE_MINUTES`) sebelum dicatat kedaluwarsa.
- Halaman share juga menampilkan nama + no. HP pemesan — bot tidak
  menampilkannya di kartu (discretion), hanya dipakai internal.

### Umum
- `tg.py` **tidak** memakai `url_with_surrogate_path_segment()` dari
  `dynamic_credentials` karena ia me-percent-encode surrogate (`hsurr%3A…`)
  sehingga proxy egress tidak mengenalinya → Telegram 404. Surrogate
  disisipkan tanpa encoding (isinya hanya `hsurr:` + hex, aman di path).
- Webhook lama (`notifikasi-order-grabfood.vercel.app`, rusak/404) dihapus via
  `deleteWebhook` pada 2026-09-29 agar long polling bisa jalan.
- Owner = chat pertama yang `/start` (dikunci di `state.json`).
- Ikon platform = **custom emoji** logo GrabFood/ShopeeFood (set
  `pantaufood_by_grabnotifsy_bot`, tipe `custom_emoji`, dibuat via
  `createNewStickerSet` dari gambar user 2026-10-09; owner set harus akun
  manusia). ID tersimpan di `emoji.json`; `rich()` di `bot.py` mengubah
  HTML → entities Telegram dan menempelkan custom emoji di tiap 🟧/🛵.
  `send_html`/`edit_html` memakainya otomatis — entities & parse_mode tidak
  bisa digabung, jadi pesan berlogo dikirim full-entities.
