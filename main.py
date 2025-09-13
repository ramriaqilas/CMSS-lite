# CMMS - Lite.py
# Telegram bot to log spare-part transactions into Google Sheets (TransaksiGudang).
#
# Features:
# - MUTASI (record IN/OUT): PartID/Nama -> Jenis -> Jumlah -> Kondisi -> Tujuan -> SAVE
# - CARI (search location): /cari -> ketik nama/PartID -> pilih kandidat -> tampilkan lokasi & visual (jika ada)

import re
import io
import json
import logging
import os
from datetime import datetime
from dotenv import load_dotenv

import pytz
from PIL import Image

try:
    from pyzbar.pyzbar import decode as qr_decode

    QR_AVAILABLE = True
except Exception:
    QR_AVAILABLE = False

import gspread
from google.oauth2.service_account import Credentials
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ====== Configuration via environment variables ======
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME", "TransaksiGudang")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta")

# Master Sparepart sheet & headers
SPAREPART_SHEET = os.getenv("SPAREPART_SHEET", "Sparepart")
SPAREPART_NAME_HEADERS = os.getenv("SPAREPART_NAME_HEADERS", "NamaPart")
SPAREPART_LOCATION_HEADERS = os.getenv("SPAREPART_LOCATION_HEADERS", "KodeLokasi")
SPAREPART_VISUAL_HEADERS = os.getenv(
    "SPAREPART_VISUAL_HEADERS",
    "Visual,Visual Management,Foto,Image,Gambar,Link Visual"
).split(",")

# Validation options
JENIS_OPTIONS = ["In", "Out"]
KONDISI_OPTIONS = ["Baru", "Used"]

# ====== Logging ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ====== Conversation states ======
PART, JENIS, JUMLAH, KONDISI, TUJUAN = range(5)  # for MUTASI
CARI_QUERY = 20  # for CARI (search)


def _now_string():
    tz = pytz.timezone(TIMEZONE)
    return datetime.now(tz).strftime("%m/%d/%y %H:%M:%S")


def _get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    info = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
    file = os.getenv("GCP_SERVICE_ACCOUNT_FILE")
    if info:
        creds = Credentials.from_service_account_info(json.loads(info), scopes=scopes)
    elif file:
        creds = Credentials.from_service_account_file(file, scopes=scopes)
    else:
        raise RuntimeError("Missing Google credentials. Set GCP_SERVICE_ACCOUNT_JSON or GCP_SERVICE_ACCOUNT_FILE.")
    return gspread.authorize(creds)


def _open_sheet(name):
    gc = _get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(name)


def _append_row_to_sheet(row_values):
    ws = _open_sheet(SHEET_NAME)
    ws.append_row(row_values, value_input_option="USER_ENTERED")


def _qr_from_image_bytes(b: bytes):
    if not QR_AVAILABLE:
        return None
    try:
        img = Image.open(io.BytesIO(b)).convert("RGB")
        results = qr_decode(img)
        if results:
            return results[0].data.decode("utf-8").strip()
        return None
    except Exception as e:
        logger.exception("QR decode failed: %s", e)
        return None


# ====== Helpers Function ======
def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', (s or '').strip().lower())


def _norm_id(s: str) -> str:
    # normalisasi PartID: uppercase + hilangkan spasi, - dan _
    return re.sub(r'[\s\-_]+', '', (s or '').strip().upper())


def _as_list(x):
    # pastikan x berupa list[str] (string comma-separated juga aman)
    if isinstance(x, (list, tuple)):
        return [str(i).strip() for i in x if str(i).strip()]
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return []
        return [p.strip() for p in s.split(",")] if "," in s else [s]
    return []


def find(syns, header):
    nh = [_norm(h) for h in header]
    ns = [_norm(x) for x in syns]
    # exact by normalized form
    for i, h in enumerate(nh):
        if h in ns:
            return i
    # partial fallback
    for i, h in enumerate(nh):
        for s in ns:
            if s and (s in h or h in s):
                return i
    return None


def _find_one(header, syns):
    # cari 1 index berdasarkan daftar sinonim (exact normalized, lalu partial)
    nh = [_norm(h) for h in header]
    ns = [_norm(x) for x in _as_list(syns)]
    # exact
    for i, h in enumerate(nh):
        if h in ns:
            return i
    # partial
    for i, h in enumerate(nh):
        for s in ns:
            if s and (s in h or h in s):
                return i
    return None


def _find_many(header, syns):
    # cari banyak kolom (untuk lokasi) berdasarkan sinonim
    out = []
    nh = [_norm(h) for h in header]
    ns = [_norm(x) for x in _as_list(syns)]
    for i, h in enumerate(nh):
        if any(s and (h == s or s in h or h in s) for s in ns):
            out.append(i)
    return out


def _tg_header_map():
    """Baca header sheet TransaksiGudang dan kembalikan index kolom penting berdasarkan nama header."""
    ws = _open_sheet(SHEET_NAME)
    rows = ws.get_all_values()
    if not rows:
        raise RuntimeError(f"Sheet {SHEET_NAME} kosong / belum ada header.")

    header = [h.strip() for h in rows[0]]

    idx = {
        "timestamp": find(["Timestamp"], header),
        "partid": find(["PartID"], header),
        "jenis": find(["Jenis"], header),
        "jumlah": find(["Jumlah"], header),
        "kondisi": find(["Kondisi"], header),
        "userid": find(["UserID"], header),
        "tujuan": find(["Tujuan/Penggunaan"], header)
    }

    missing = [k for k, v in idx.items() if v is None]
    if missing:
        raise RuntimeError(f"Header TransaksiGudang tidak ditemukan untuk: {', '.join(missing)}")

    return header, idx


def _sp_header_map():
    """Petakan index kolom penting di sheet Sparepart (PartID, Nama, Lokasi*, Visual*)."""
    ws = _open_sheet(SPAREPART_SHEET)
    rows = ws.get_all_values()
    if not rows:
        raise RuntimeError(f"Sheet {SPAREPART_SHEET} kosong / belum ada header.")
    header = [h.strip() for h in rows[0]]
    data = rows[1:]

    # sinonim default + bisa di-override via .env
    syn_pid = _as_list(os.getenv("SPARE_COL_PARTID", "PartID,Part ID,Kode,Kode Part,ID Part,ID Barang,ID"))
    syn_name = _as_list(os.getenv("SPARE_COL_NAME", "NamaPart,Nama Barang,Nama,Deskripsi,Item,Part Name"))
    syn_locs = _as_list(os.getenv("SPARE_COL_LOCATIONS", "KodeLokasi,Lokasi,Rak,Tingkat,Nomor"))
    syn_visual = _as_list(os.getenv("SPARE_COL_VISUAL", "Visual,Visual Management,Foto,Image,Gambar,Link Visual"))

    pid_i = _find_one(header, syn_pid)
    name_i = _find_one(header, syn_name)
    loc_is = _find_many(header, syn_locs)
    visual_i = _find_one(header, syn_visual)

    # hanya wajib ada PartID ATAU Nama
    if pid_i is None and name_i is None:
        raise RuntimeError(f"Tidak menemukan kolom PartID maupun Nama pada '{SPAREPART_SHEET}'. Header: {header}")

    return header, data, {"pid": pid_i, "name": name_i, "locs": loc_is, "visual": visual_i}


def _load_sparepart():
    """Return header (list[str]) and data (list[list[str]]) from Sparepart sheet."""
    wsp = _open_sheet(SPAREPART_SHEET)
    rows = wsp.get_all_values()
    if not rows:
        return [], []
    return [h.strip() for h in rows[0]], rows[1:]


def _find_col_indices(header):
    nh = [_norm(h) for h in header]

    def find_idx(cands):
        cands = [c for c in (cands or []) if str(c).strip()]
        # exact normalized
        for i, h in enumerate(nh):
            if any(_norm(c) == h for c in cands):
                return i
        # partial fallback
        for i, h in enumerate(nh):
            if any(_norm(c) and (_norm(c) in h or h in _norm(c)) for c in cands):
                return i
        return None

    partid_idx = find_idx(["PartID"])
    nama_idx = find_idx(SPAREPART_NAME_HEADERS)

    # lokasi & visual tetap seperti semula
    loc_indices = []
    for cand in SPAREPART_LOCATION_HEADERS:
        i = find_idx([cand])
        if i is not None and i not in loc_indices:
            loc_indices.append(i)
    visual_idx = find_idx(SPAREPART_VISUAL_HEADERS)

    return partid_idx, nama_idx, loc_indices, visual_idx


def _try_resolve_partid_from_name_or_id(text: str):
    """
    Input: PartID ATAU Nama.
    Return:
      - (pid, nama, None)                    -> ketemu tunggal
      - (None, [(pid,nm), ...], 'multiple')  -> kandidat >1
      - (text, None, 'lenient')              -> tak ketemu (akan disimpan apa adanya)
    """
    q_raw = (text or "").strip()
    if not q_raw:
        return q_raw, None, "lenient"

    try:
        header, data, idx = _sp_header_map()
    except Exception:
        return q_raw, None, "lenient"

    pid_i, name_i = idx["pid"], idx["name"]

    # 1) exact PartID (pakai _norm_id biar toleran spasi/-/_ & case)
    if pid_i is not None:
        qn = _norm_id(q_raw)
        for r in data:
            if len(r) > pid_i:
                pid = (r[pid_i] or "").strip()
                if pid and _norm_id(pid) == qn:
                    nm = (r[name_i].strip() if name_i is not None and len(r) > name_i else "")
                    return pid, (nm or None), None

    # 2) contains by Nama (case-insensitive)
    if name_i is not None:
        ql = q_raw.lower()
        cands = []
        for r in data:
            if len(r) > name_i:
                nm = (r[name_i] or "").strip()
                if nm and ql in nm.lower():
                    pid = (r[pid_i].strip() if pid_i is not None and len(r) > pid_i else None)
                    if pid:
                        cands.append((pid, nm))
        if len(cands) == 1:
            return cands[0][0], cands[0][1], None
        if len(cands) > 1:
            return None, cands[:25], "multiple"

    # 3) fallback lenient
    return q_raw, None, "PartID tidak ditemukan di master; disimpan apa adanya."


def _search_sparepart(query: str):
    """Cari by PartID/Nama (contains). Kembalikan list dict agar mudah ditampilkan di /cari."""
    header, data, idx = _sp_header_map()
    pid_i, name_i, loc_is, visual_i = idx["pid"], idx["name"], idx["locs"], idx["visual"]
    q = (query or "").strip().lower()
    results = []

    for r in data:
        pid = r[pid_i].strip() if pid_i is not None and len(r) > pid_i else ""
        nm = r[name_i].strip() if name_i is not None and len(r) > name_i else ""
        if (q in (pid or "").lower()) or (q in (nm or "").lower()):
            # kumpulkan kolom lokasi (bisa banyak)
            loc = {}
            for i in loc_is:
                if len(r) > i and r[i].strip():
                    loc[header[i]] = r[i].strip()
            visual = r[visual_i].strip() if visual_i is not None and len(r) > visual_i else ""
            results.append({"PartID": pid, "Nama": nm, "Lokasi": loc, "Visual": visual})
    return results, header, (pid_i, name_i, loc_is, visual_i)


async def _handle_partid_input(text: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    partid, info, warn = _try_resolve_partid_from_name_or_id(text)
    if warn == "multiple" and isinstance(info, list):
        buttons = [[InlineKeyboardButton((nm or pid)[:50], callback_data=f"pickpid:{pid}")]
                   for pid, nm in info[:10]]
        await update.message.reply_text("Ditemukan beberapa kandidat. Silakan pilih:",
                                        reply_markup=InlineKeyboardMarkup(buttons))
        return PART
    if warn and warn not in ("multiple",):
        await update.message.reply_text(f"⚠️ {warn}")

    context.user_data["partid"] = partid
    keyboard = [[InlineKeyboardButton(j, callback_data=f"jenis:{j}")] for j in JENIS_OPTIONS]
    await update.message.reply_text("Pilih *Jenis* (In/Out):", parse_mode="Markdown",
                                    reply_markup=InlineKeyboardMarkup(keyboard))
    return JENIS


# ====== MUTASI Handlers ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Halo! Perintah tersedia:\n"
        "- /mutasi — catat IN/OUT (PartID/Nama → Jenis → Jumlah → Kondisi → Tujuan → simpan)\n"
        "- /cari — cari sparepart (by PartID atau Nama)"
    )


async def mutasi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Kirim *foto QR* atau *ketik PartID/Nama Barang*.",
        parse_mode="Markdown"
    )
    return PART


async def partid_from_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        return PART
    file = await context.bot.get_file(update.message.photo[-1].file_id)
    b = await file.download_as_bytearray()
    text = _qr_from_image_bytes(b)
    if not text:
        await update.message.reply_text("QR tidak terbaca. Foto ulang atau ketik PartID/Nama.")
        return PART
    return await _handle_partid_input(text, update, context)


async def partid_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Masukan kosong. Kirim foto QR atau ketik PartID/Nama.")
        return PART
    return await _handle_partid_input(text, update, context)


async def pick_partid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pid = query.data.split(":", 1)
    context.user_data["partid"] = pid
    await query.edit_message_text(f"PartID dipilih: {pid}")
    keyboard = [[InlineKeyboardButton(j, callback_data=f"jenis:{j}")] for j in JENIS_OPTIONS]
    await query.message.reply_text("Pilih *Jenis* (IN/OUT):", parse_mode="Markdown",
                                   reply_markup=InlineKeyboardMarkup(keyboard))
    return JENIS


async def jenis_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, val = query.data.split(":", 1)
    context.user_data["jenis"] = val
    await query.edit_message_text(f"Jenis: {val}")
    await query.message.reply_text("Masukkan *Jumlah* (angka > 0).", parse_mode="Markdown")
    return JUMLAH


async def jumlah_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        qty = int(txt)
        if qty <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Jumlah tidak valid. Masukkan nilai > 0.")
        return JUMLAH
    context.user_data["jumlah"] = qty
    keyboard = [[InlineKeyboardButton(k, callback_data=f"kondisi:{k}")] for k in KONDISI_OPTIONS]
    await update.message.reply_text(
        "Pilih *Kondisi*:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )
    return KONDISI


async def kondisi_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, val = query.data.split(":", 1)
    context.user_data["kondisi"] = val
    await query.edit_message_text(f"Kondisi: {val}")
    await query.message.reply_text("Tulis *Tujuan/Penggunaan* (singkat).", parse_mode="Markdown")
    return TUJUAN


def _save_row(context, user_id: int):
    data = context.user_data.copy()
    ts = _now_string()

    # Pastikan Jenis eksak "In" / "Out"
    jenis_raw = str(data.get("jenis", "")).strip()
    if jenis_raw.lower() == "in":
        jenis_val = "In"
    elif jenis_raw.lower() == "out":
        jenis_val = "Out"
    else:
        jenis_val = jenis_raw  # fallback

    header, idx = _tg_header_map()
    row = [""] * len(header)

    row[idx["timestamp"]] = ts
    row[idx["partid"]] = str(data.get("partid", ""))
    row[idx["jenis"]] = jenis_val
    row[idx["jumlah"]] = int(data.get("jumlah", 0)) if data.get("jumlah") else ""
    row[idx["kondisi"]] = str(data.get("kondisi", ""))
    row[idx["userid"]] = str(user_id)
    row[idx["tujuan"]] = str(data.get("tujuan", ""))

    _append_row_to_sheet(row)
    return ts, row


async def tujuan_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tujuan = update.message.text.strip()
    context.user_data["tujuan"] = tujuan
    user_id = update.effective_user.id
    try:
        ts, row = _save_row(context, user_id)
        await update.message.reply_text(
            f"✅ Tersimpan ke *{SHEET_NAME}*\n"
            f"Waktu: {ts}\n"
            f"PartID: {row[2]}\n"
            f"Jenis: {row[3]} | Jumlah: {row[4]} | Kondisi: {row[5]}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.exception("Append failed: %s", e)
        await update.message.reply_text(f"❌ Gagal menyimpan: {e}")
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Dibatalkan.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ====== CARI (Search) Handlers ======
async def cari(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ketik *nama barang* atau *PartID* yang ingin dicari.",
        parse_mode="Markdown"
    )
    return CARI_QUERY


def _format_location(loc_dict):
    if not loc_dict:
        return "-"
    # Prioritize showing a main field if present
    main = None
    for key in SPAREPART_LOCATION_HEADERS:
        if key in loc_dict and loc_dict[key]:
            main = f"{key}: {loc_dict[key]}"
            break
    # Then show extras
    extras = [f"{k}={v}" for k, v in loc_dict.items() if not main or f"{k}: {v}" != main]
    if main and extras:
        return main + " | " + " ".join(extras)
    return main or " | ".join(extras)


def _format_location_pretty(loc: dict):
    """Format dua baris:
    - Baris 1: Lokasi: <KodeLokasi>
    - Baris 2: Rak = X | Tingkat = Y | Jollybox = ZZ
    """
    if not isinstance(loc, dict):
        loc = {}

    kode = (loc.get("KodeLokasi") or "").strip()
    rak = (loc.get("Rak") or "").strip()
    tingkat = (loc.get("Tingkat") or "").strip()
    nomor = (loc.get("Nomor") or "").strip()

    # Nomor/Jollybox jadi 2 digit bila angka
    if nomor.isdigit():
        try:
            nomor = f"{int(nomor):02d}"
        except Exception:
            pass

    line1 = f"Lokasi: {kode or '-'}"
    details = []
    if rak:
        details.append(f"Rak = {rak}")
    if tingkat:
        details.append(f"Tingkat = {tingkat}")
    if nomor:
        details.append(f"Jollybox = {nomor}")
    line2 = " | ".join(details) if details else ""
    return line1, line2


async def cari_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.message.text.strip()
    if len(q) < 2:
        await update.message.reply_text("Input terlalu pendek. Input minimal 2 huruf/angka.")
        return CARI_QUERY

    try:
        results, header, _ = _search_sparepart(q)
    except Exception as e:
        logger.exception("Search failed: %s", e)
        await update.message.reply_text(f"❌ Gagal mencari: {e}")
        return ConversationHandler.END

    if not results:
        await update.message.reply_text("Tidak ada hasil. Coba kata kunci lain.")
        return CARI_QUERY

    if len(results) == 1:
        r = results[0]
        visual = r.get("Visual") or "(Visual belum tersedia)"

        # ganti dari _format_location(...) ke:
        line1, line2 = _format_location_pretty(r.get("Lokasi") or {})

        msg = (
            f"**Hasil**\n"
            f"PartID: `{r['PartID']}`\n"
            f"Nama: {r['Nama'] or '-'}\n"
            f"{line1}\n"
            f"{line2}\n"
            f"Visual: {visual}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return ConversationHandler.END

    # Multiple candidates: ask to pick (limit 10)
    buttons = []
    for r in results[:10]:
        label = (r.get('Nama') or r.get('PartID'))[:50]
        buttons.append([InlineKeyboardButton(label, callback_data=f"caripick:{r['PartID']}")])
    await update.message.reply_text(
        "Ditemukan beberapa kandidat. Pilih salah satu:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    # Store a cache of search by PartID for quick resolve
    context.user_data["cari_cache"] = {r["PartID"]: r for r in results[:50]}
    return CARI_QUERY


async def cari_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pid = query.data.split(":", 1)

    cache = context.user_data.get("cari_cache") or {}
    r = cache.get(pid)

    if not r:
        results, _, _ = _search_sparepart(pid)
        r = next((it for it in results if it["PartID"] == pid), None)

    if not r:
        await query.edit_message_text("Data tidak ditemukan. Coba cari lagi dengan /cari.")
        return ConversationHandler.END

    lokasi_text = _format_location(r.get("Lokasi") or {})
    visual = r.get("Visual") or "(Visual belum tersedia)"
    line1, line2 = _format_location_pretty(r.get("Lokasi") or {})

    msg = (
        f"**Hasil**\n"
        f"PartID: `{r['PartID']}`\n"
        f"Nama: {r['Nama'] or '-'}\n"
        f"{line1}\n"
        f"{line2}\n"
        f"Visual: {visual}"
    )
    await query.edit_message_text(msg, parse_mode="Markdown")
    context.user_data.pop("cari_cache", None)
    return ConversationHandler.END


# ====== Debug Handlers ======
async def debug_tg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        header, idx = _tg_header_map()
        msg = "Header TransaksiGudang:\n" + "\n".join([f"{i + 1}. {h}" for i, h in enumerate(header)]) + \
              "\n\nIndex:\n" + "\n".join([f"{k}: {v}" for k, v in idx.items()])
    except Exception as e:
        msg = f"Error: {e}"
    await update.message.reply_text(f"```\n{msg}\n```", parse_mode="Markdown")


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN not set")
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID not set")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # MUTASI flow
    conv_mutasi = ConversationHandler(
        entry_points=[CommandHandler("mutasi", mutasi)],
        states={
            PART: [
                MessageHandler(filters.PHOTO, partid_from_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, partid_from_text),
                CallbackQueryHandler(pick_partid, pattern=r"^pickpid:"),
            ],
            JENIS: [CallbackQueryHandler(jenis_chosen, pattern=r"^jenis:")],
            JUMLAH: [MessageHandler(filters.TEXT & ~filters.COMMAND, jumlah_input)],
            KONDISI: [CallbackQueryHandler(kondisi_chosen, pattern=r"^kondisi:")],
            TUJUAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, tujuan_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        name="mutasi_conv",
        persistent=False,
    )

    # CARI flow
    conv_cari = ConversationHandler(
        entry_points=[CommandHandler("cari", cari)],
        states={
            CARI_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cari_query),
                CallbackQueryHandler(cari_pick, pattern=r"^caripick:"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        name="cari_conv",
        persistent=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_mutasi)
    app.add_handler(conv_cari)
    app.add_handler(CommandHandler("debug_tg", debug_tg))

    logger.info("Bot started. Waiting for updates...")
    app.run_polling()


if __name__ == "__main__":
    main()
