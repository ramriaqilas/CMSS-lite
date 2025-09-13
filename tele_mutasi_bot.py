# tele_mutasi_bot.py
# Telegram bot to log spare-part transactions into Google Sheets (TransaksiGudang).
#
# Features:
# - MUTASI (record IN/OUT): PartID/Nama -> Jenis -> Jumlah -> Kondisi -> Tujuan -> SAVE
# - CARI (search location): /cari -> ketik nama/PartID -> pilih kandidat -> tampilkan lokasi & visual (jika ada)

import asyncio
import io
import json
import logging
import os
from datetime import datetime

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
JENIS_OPTIONS = ["IN", "OUT"]
KONDISI_OPTIONS = os.getenv("KONDISI_OPTIONS", "baru,used").split(",")

# ====== Logging ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ====== Conversation states ======
PART, JENIS, JUMLAH, KONDISI, TUJUAN = range(5)           # for MUTASI
CARI_QUERY = 20                                           # for CARI (search)

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

# ====== Sparepart Helpers ======
def _load_sparepart():
    """Return header (list[str]) and data (list[list[str]]) from Sparepart sheet."""
    wsp = _open_sheet(SPAREPART_SHEET)
    rows = wsp.get_all_values()
    if not rows:
        return [], []
    return [h.strip() for h in rows[0]], rows[1:]

def _find_col_indices(header):
    """Find indices for common fields in Sparepart sheet."""
    def find_idx_exact(candidates):
        for cand in candidates:
            for i, h in enumerate(header):
                if h.strip().lower() == cand.strip().lower():
                    return i
        return None
    partid_idx = find_idx_exact("PartID")
    nama_idx = find_idx_exact(SPAREPART_NAME_HEADERS)
    # location can be multiple columns; collect all indices that match
    loc_indices = []
    for cand in SPAREPART_LOCATION_HEADERS:
        for i, h in enumerate(header):
            if h.strip().lower() == cand.strip().lower():
                if i not in loc_indices:
                    loc_indices.append(i)
    visual_idx = find_idx_exact(SPAREPART_VISUAL_HEADERS)
    return partid_idx, nama_idx, loc_indices, visual_idx

def _try_resolve_partid_from_name_or_id(text: str):
    query = text.strip()
    try:
        header, data = _load_sparepart()
        if not header:
            return query, None, "Master Sparepart kosong; menyimpan PartID apa adanya."
        partid_idx, nama_idx, _loc_idxs, _visual_idx = _find_col_indices(header)
        if partid_idx is not None:
            for r in data:
                if len(r) > partid_idx and r[partid_idx].strip().lower() == query.lower():
                    partid = r[partid_idx].strip()
                    nm = r[nama_idx].strip() if nama_idx is not None and len(r) > nama_idx else None
                    return partid, nm, None
        # search by name contains
        if nama_idx is not None:
            candidates = []
            for r in data:
                if len(r) > nama_idx and query.lower() in r[nama_idx].strip().lower():
                    pid = r[partid_idx].strip() if partid_idx is not None and len(r) > partid_idx else None
                    nm = r[nama_idx].strip()
                    if pid:
                        candidates.append((pid, nm))
            if len(candidates) == 1:
                return candidates[0][0], candidates[0][1], None
            elif len(candidates) > 1:
                return None, candidates, "multiple"
        return query, None, "PartID tidak ditemukan di master; disimpan apa adanya."
    except Exception as e:
        logger.warning("Sparepart lookup skipped: %s", e)
        return query, None, "Gagal akses master Sparepart; menyimpan PartID apa adanya."

def _search_sparepart(query: str):
    """Return search results (list of dict) for Sparepart by PartID or Name (contains)."""
    header, data = _load_sparepart()
    partid_idx, nama_idx, loc_idxs, visual_idx = _find_col_indices(header)
    if not header or (partid_idx is None and nama_idx is None):
        return [], header, (partid_idx, nama_idx, loc_idxs, visual_idx)

    q = query.strip().lower()
    results = []

    for r in data:
        pid = r[partid_idx].strip() if partid_idx is not None and len(r) > partid_idx else ""
        nm  = r[nama_idx].strip() if nama_idx is not None and len(r) > nama_idx else ""
        if (q in pid.lower()) or (q in nm.lower()):
            # Collect location fields
            loc = {}
            for idx in loc_idxs:
                if len(r) > idx and r[idx].strip():
                    loc_name = header[idx]
                    loc_val = r[idx].strip()
                    loc[loc_name] = loc_val
            visual = r[visual_idx].strip() if visual_idx is not None and len(r) > visual_idx else ""
            results.append({
                "PartID": pid,
                "Nama": nm,
                "Lokasi": loc,
                "Visual": visual
            })
    return results, header, (partid_idx, nama_idx, loc_idxs, visual_idx)

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
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    b = await file.download_as_bytearray()
    text = _qr_from_image_bytes(b)
    if not text:
        await update.message.reply_text("QR tidak terbaca. Silakan foto ulang atau ketik PartID/Nama manual.")
        return PART

    partid, info, warn = _try_resolve_partid_from_name_or_id(text)
    if warn == "multiple" and isinstance(info, list):
        buttons = [[InlineKeyboardButton(f"{pid} — {nm}", callback_data=f"pickpid:{pid}")]
                   for pid, nm in info[:10]]
        await update.message.reply_text(
            "Ditemukan beberapa kandidat. Silakan pilih salah satu:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return PART
    if warn and warn not in ("multiple",):
        await update.message.reply_text(f"⚠️ {warn}")

    context.user_data["partid"] = partid
    keyboard = [[InlineKeyboardButton(j, callback_data=f"jenis:{j}")] for j in JENIS_OPTIONS]
    await update.message.reply_text("Pilih *Jenis* (IN/OUT):", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return JENIS

async def partid_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Masukan kosong. Kirim foto QR atau Ketik PartID/Nama.")
        return PART

    partid, info, warn = _try_resolve_partid_from_name_or_id(text)
    if warn == "multiple" and isinstance(info, list):
        buttons = [[InlineKeyboardButton(f"{pid} — {nm}", callback_data=f"pickpid:{pid}")]
                   for pid, nm in info[:10]]
        await update.message.reply_text(
            "Ditemukan beberapa kandidat. Silakan pilih salah satu:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return PART
    if warn and warn not in ("multiple",):
        await update.message.reply_text(f"⚠️ {warn}")

    context.user_data["partid"] = partid
    keyboard = [[InlineKeyboardButton(j, callback_data=f"jenis:{j}")] for j in JENIS_OPTIONS]
    await update.message.reply_text("Pilih *Jenis* (IN/OUT):", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return JENIS

async def pick_partid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pid = query.data.split(":", 1)
    context.user_data["partid"] = pid
    await query.edit_message_text(f"PartID dipilih: {pid}")
    keyboard = [[InlineKeyboardButton(j, callback_data=f"jenis:{j}")] for j in JENIS_OPTIONS]
    await query.message.reply_text("Pilih *Jenis* (IN/OUT):", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return JENIS

async def jenis_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, val = query.data.split(":", 1)
    context.user_data["jenis"] = val.upper()
    await query.edit_message_text(f"Jenis: {val.upper()}")
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
    keyboard = [[InlineKeyboardButton(k, callback_data=f"kondisi:{k}") ] for k in KONDISI_OPTIONS]
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
    row = [
        ts,
        str(data.get("partid", "")),
        str(data.get("jenis", "")),
        int(data.get("jumlah", 0)),
        str(data.get("kondisi", "")),
        str(user_id),
        str(data.get("tujuan", "")),
    ]
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
            f"PartID: {row[1]}\n"
            f"Jenis: {row[2]} | Jumlah: {row[3]} | Kondisi: {row[4]}",
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
        lokasi_text = _format_location(r.get("Lokasi") or {})
        visual = r.get("Visual") or "(Visual belum tersedia)"
        msg = (
            f"**Hasil**\n"
            f"PartID: `{r['PartID']}`\n"
            f"Nama: {r['Nama'] or '-'}\n"
            f"Lokasi: {lokasi_text}\n"
            f"Visual: {visual}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return ConversationHandler.END

    # Multiple candidates: ask to pick (limit 10)
    buttons = []
    for r in results[:10]:
        label = f"{r['PartID']} — {r['Nama'][:40]}" if r['Nama'] else r['PartID']
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
    msg = (
        f"**Hasil**\n"
        f"PartID: `{r['PartID']}`\n"
        f"Nama: {r['Nama'] or '-'}\n"
        f"Lokasi: {lokasi_text}\n"
        f"Visual: {visual}"
    )
    await query.edit_message_text(msg, parse_mode="Markdown")
    context.user_data.pop("cari_cache", None)
    return ConversationHandler.END

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

    logger.info("Bot started. Waiting for updates...")
    app.run_polling()

if __name__ == "__main__":
    main()
