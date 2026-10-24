"""بوت تيليغرام لتنزيل الفيديوهات العامة المسموح بتنزيلها.

يعمل عبر Webhook على Render. ضع TELEGRAM_BOT_TOKEN في متغيرات البيئة
على Render فقط، ولا تنشره أبداً على GitHub.
"""

import asyncio
import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("video_bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
MAX_BYTES = 49 * 1024 * 1024


def telecharger_video(url: str, dossier: str) -> tuple[Path, str]:
    options = {
        "format": "best[ext=mp4][height<=720]/best[ext=mp4]/best",
        "outtmpl": str(Path(dossier) / "video.%(ext)s"),
        "noplaylist": True,
        "max_filesize": MAX_BYTES,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
    }
    with yt_dlp.YoutubeDL(options) as client:
        info = client.extract_info(url, download=True)
        titre = (info or {}).get("title", "فيديو")
    fichiers = [
        p for p in Path(dossier).glob("video.*")
        if p.is_file() and not p.name.endswith((".part", ".ytdl"))
    ]
    if not fichiers:
        raise RuntimeError("لم يتم إنشاء ملف فيديو.")
    return max(fichiers, key=lambda p: p.stat().st_size), str(titre)


async def debut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "👋 أهلاً وسهلاً! أرسل رابط فيديو عام مسموح لك بتنزيله، "
            "وسأحاول إرسال الملف إليك.\n📦 الحد الأقصى لحجم الفيديو: 49 ميغابايت."
        )


async def lien_recu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    texte = update.message.text or ""
    correspondance = re.search(r"https?://[^\s<>]+", texte)
    if not correspondance:
        await update.message.reply_text("🔗 أرسل رابط فيديو يبدأ بـ https://")
        return
    url = correspondance.group(0).rstrip(".,;)")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        await update.message.reply_text("❌ الرابط غير صالح.")
        return
    statut = await update.message.reply_text("⏳ جارٍ تنزيل الفيديو…")
    try:
        with tempfile.TemporaryDirectory(prefix="telegram_video_") as dossier:
            fichier, titre = await asyncio.to_thread(telecharger_video, url, dossier)
            if fichier.stat().st_size > MAX_BYTES:
                await statut.edit_text("❌ حجم الفيديو كبير جداً. الحد الأقصى 49 ميغابايت.")
                return
            await statut.edit_text("📤 جارٍ إرسال الفيديو عبر تيليغرام…")
            with fichier.open("rb") as flux:
                if fichier.suffix.lower() == ".mp4":
                    await update.message.reply_video(
                        video=flux,
                        supports_streaming=True,
                        read_timeout=180,
                        write_timeout=180,
                    )
                else:
                    await update.message.reply_document(
                        document=flux,
                        read_timeout=180,
                        write_timeout=180,
                    )
            await statut.delete()
    except Exception:
        logger.exception("فشل تنزيل الفيديو أو إرساله")
        await statut.edit_text(
            "❌ تعذّر تنزيل الفيديو. قد يطلب الموقع تسجيل الدخول، "
            "أو يمنع التنزيل، أو قد يكون الملف كبيراً جداً."
        )


def main() -> None:
    if not TOKEN:
        raise SystemExit("متغير TELEGRAM_BOT_TOKEN غير موجود في إعدادات Render.")

    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base_url.startswith("https://"):
        raise SystemExit("متغير RENDER_EXTERNAL_URL غير موجود. يجب استخدام Web Service على Render.")

    secret = hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()[:40]
    chemin = f"telegram/{secret}"
    port = int(os.environ.get("PORT", "10000"))

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", debut))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lien_recu))
    logger.info("Démarrage du bot sur le port %s (mode webhook).", port)
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=chemin,
        webhook_url=f"{base_url}/{chemin}",
        secret_token=secret,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
