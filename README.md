# Pantau GrabFood — Bot Telegram Pribadi

Bot Telegram pribadi untuk memantau pesanan GrabFood lewat link share.
Tempel link `https://app.grab.com/s/xxxxxx` atau
`https://sharelocation.grab.com/o/xxxxxx`, bot memantau posisi driver tiap
10 detik lewat kartu status + live location, lengkap dengan notifikasi.

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
2. Tempel link share dari aplikasi Grab — `https://app.grab.com/s/xxxxxx`
   atau `https://sharelocation.grab.com/o/xxxxxx`
3. Bot mengirim:
   - **Kartu status** (memperbarui diri tiap 10 detik): status pesanan, nama
     driver + rating, kendaraan + plat, asal, tujuan, jarak driver→tujuan,
     🗺️ progress bar perjalanan, perkiraan tiba. Tombol: ⏹ Stop,
     📍 Buka peta driver.
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
| `state.json` | owner_id, offset getUpdates, daftar pantauan aktif (tahan restart), antrian pengingat rating, settings |
| `history.json` | Riwayat pesanan selesai (maks 100): merchant, driver, rating, durasi, selisih vs estimasi |
| `bot.pid` / `bot.log` | pid proses + log |
| `watchdog.sh` | Dipanggil cron tiap 5 mnt; start ulang bot bila mati |

## Catatan teknis

- API Grab (`sharemyride/.../bookingdetails`) tidak butuh auth; ditemukan dari
  bundle JS `sharelocation.grab.com` (`config.json` → `uri: https://api.grab.com`).
- `tg.py` **tidak** memakai `url_with_surrogate_path_segment()` dari
  `dynamic_credentials` karena ia me-percent-encode surrogate (`hsurr%3A…`)
  sehingga proxy egress tidak mengenalinya → Telegram 404. Surrogate
  disisipkan tanpa encoding (isinya hanya `hsurr:` + hex, aman di path).
- Webhook lama (`notifikasi-order-grabfood.vercel.app`, rusak/404) dihapus via
  `deleteWebhook` pada 2026-09-29 agar long polling bisa jalan.
- Owner = chat pertama yang `/start` (dikunci di `state.json`).
