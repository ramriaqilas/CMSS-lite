# Telegram Mutasi Sparepart Bot

## Perintah
- `/mutasi` — catat mutasi IN/OUT ke sheet `TransaksiGudang`  
  **Flow:** PartID/Nama → Jenis → Jumlah → Kondisi → Tujuan → **langsung simpan**
- `/cari` — cari lokasi sparepart di sheet `Sparepart`  
  **Flow:** Ketik kata kunci (PartID/Nama) → Jika banyak kandidat pilih salah satu → Bot menampilkan **Kode Lokasi** dan **Visual** (jika tersedia).

## Kolom `TransaksiGudang` yang ditulis
1. Timestamp — `MM/DD/YY HH:MM:SS` (Asia/Jakarta)
2. PartID — dari QR atau input manual (bisa hasil lookup dari nama)
3. Jenis — IN / OUT
4. Jumlah — integer > 0
5. Kondisi — default: baru, used, repair, scrap (bisa diubah via env `KONDISI_OPTIONS`)
6. UserID — ID Telegram penginput
7. Tujuan/Penggunaan — teks singkat

## Konfigurasi Sheet Master Sparepart
Env vars (optional untuk lookup & pencarian):
```
SPAREPART_SHEET=Sparepart
SPAREPART_NAME_HEADERS="Nama Barang,Nama_Barang,Nama"
SPAREPART_LOCATION_HEADERS="Lokasi,Kode Lokasi,Location,Location Code,Kode_Lokasi,Rak,Kolom,Baris,Area,Slot,Bin"
SPAREPART_VISUAL_HEADERS="Visual,Visual Management,Foto,Image,Gambar,Link Visual"
```
> Bot akan membaca kolom-kolom di atas bila ada. Jika tidak ada, lokasi/visual akan ditampilkan sebagai “-” atau “(Visual belum tersedia)”.

## Setup singkat
1. Dapatkan `TELEGRAM_TOKEN` dari @BotFather  
2. Siapkan Service Account Google & bagikan spreadsheet ke email SA (Editor)  
3. Buat `.env` dari `.env.example`, isi data, lalu:
   ```bash
   pip install -r requirements.txt
   python tele_mutasi_bot.py
   ```

## Catatan
- Jika QR gagal terbaca, tetap bisa input manual.
- Mode validasi PartID bisa dibuat ketat (wajib ada di master) atau longgar (tetap simpan). Minta kami kalau ingin diaktifkan mode ketat.
