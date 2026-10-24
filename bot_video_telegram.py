"""بوت تيليغرام عربي: اختيار فيديو MP4 أو موسيقى MP3.

يعمل على Render (Webhook). يستخدم yt-dlp و FFmpeg الذي توفره imageio-ffmpeg.
حتى 150 ميغابايت في التنزيل، مع إرسال ملفات/أجزاء أقل من 50 ميغابايت.
احفظ TELEGRAM_BOT_TOKEN كمتغير سري في Render فقط.
"""

import asyncio
import hashlib
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import imageio_ffmpeg
import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("video_audio_bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024
MAX_SEND_BYTES = 47 * 1024 * 1024  # marge sous les 50 Mo de Bot API
MAX_PARTS = 20
JOBS = asyncio.Semaphore(1)  # ne pas surcharger l'instance Render gratuite
ACTIVE_USERS = set()
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def clavier_choix() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 فيديو", callback_data="format:video"),
        InlineKeyboardButton("🎵 موسيقى MP3", callback_data="format:audio"),
    ]])


def extraire_url(texte: str) -> str | None:
    match = re.search(r"https?://[^\s<>]+", texte)
    if not match:
        return None
    url = match.group(0).rstrip(".,;)]}")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return url


def telecharger_media(url: str, dossier: str, mode: str) -> tuple[Path, str, float]:
    """Télécharge la vidéo ou sa meilleure piste audio; pas de contenu privé/DRM."""
    opts = {
        "format": (
            "bestaudio/best" if mode == "audio"
            else "best[ext=mp4][height<=720]/best[ext=mp4]/best"
        ),
        "outtmpl": str(Path(dossier) / "source.%(ext)s"),
        "noplaylist": True,
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
    }
    with yt_dlp.YoutubeDL(opts) as client:
        info = client.extract_info(url, download=True) or {}
    fichiers = [
        p for p in Path(dossier).glob("source.*")
        if p.is_file() and not p.name.endswith((".part", ".ytdl"))
    ]
    if not fichiers:
        raise RuntimeError("لم يتم إنشاء ملف من الرابط.")
    source = max(fichiers, key=lambda p: p.stat().st_size)
    if source.stat().st_size > MAX_DOWNLOAD_BYTES:
        raise ValueError("الملف يتجاوز 150 ميغابايت.")
    return source, str(info.get("title") or "ملف"), float(info.get("duration") or 0)


def convertir_mp3(source: Path, dossier: str) -> Path:
    """Extraire uniquement le son et l'encoder en MP3 128 kbit/s."""
    sortie = Path(dossier) / "musique.mp3"
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), "-map", "0:a:0", "-vn",
        "-c:a", "libmp3lame", "-b:a", "128k", str(sortie),
    ]
    resultat = subprocess.run(cmd, capture_output=True, text=True, timeout=900, check=False)
    if resultat.returncode != 0 or not sortie.exists() or sortie.stat().st_size < 100:
        raise RuntimeError("تحويل MP3: " + resultat.stderr[-700:])
    return sortie


def connaitre_duree(fichier: Path) -> float:
    resultat = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(fichier)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", resultat.stderr + resultat.stdout)
    if not m:
        raise RuntimeError("تعذّر تحديد مدة الملف لتقسيمه.")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def creer_partie(source: Path, debut: float, longueur: float, sortie: Path, mode: str) -> None:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-ss", f"{debut:.3f}", "-i", str(source), "-t", f"{longueur:.3f}"]
    if mode == "audio":
        # Audio déjà converti en MP3; le remuxage suffit pour créer chaque partie.
        cmd += ["-map", "0:a:0", "-vn", "-c:a", "copy"]
    else:
        # Réencodage en MP4 autonome, lisible dans Telegram.
        cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
                "-preset", "ultrafast", "-crf", "28", "-c:a", "aac",
                "-b:a", "96k", "-movflags", "+faststart"]
    cmd.append(str(sortie))
    resultat = subprocess.run(cmd, capture_output=True, text=True, timeout=360, check=False)
    if resultat.returncode != 0 or not sortie.exists() or sortie.stat().st_size < 100:
        raise RuntimeError("FFmpeg: " + resultat.stderr[-700:])


def preparer_parties(source: Path, duree: float, dossier: str, mode: str) -> list[Path]:
    """Coupe les MP3/vidéos volumineux en plusieurs fichiers de moins de 47 MiB."""
    total = source.stat().st_size
    if total <= MAX_SEND_BYTES:
        return [source]
    if duree <= 0:
        duree = connaitre_duree(source)
    if duree <= 0:
        raise RuntimeError("مدة الملف غير صالحة.")

    cible = max(0.15, duree * MAX_SEND_BYTES * 0.85 / total)
    position = 0.0
    morceaux: list[Path] = []
    extension = ".mp3" if mode == "audio" else ".mp4"
    dest = Path(dossier) / "parties"
    dest.mkdir(exist_ok=True)
    while position < duree - 0.05:
        if len(morceaux) >= MAX_PARTS:
            raise RuntimeError("عدد الأجزاء المطلوبة كبير جداً.")
        tentative = min(duree - position, cible)
        sortie = dest / f"partie_{len(morceaux)+1:02d}{extension}"
        reussi = False
        for _ in range(9):
            sortie.unlink(missing_ok=True)
            try:
                creer_partie(source, position, tentative, sortie, mode)
                taille = sortie.stat().st_size
                if taille <= MAX_SEND_BYTES:
                    reussi = True
                    break
                tentative = max(0.15, tentative * min(0.8, MAX_SEND_BYTES / taille * 0.85))
            except (RuntimeError, subprocess.TimeoutExpired):
                tentative = max(0.15, tentative * 0.7)
        if not reussi:
            raise RuntimeError("تعذّر تقسيم الملف إلى أجزاء تناسب تيليغرام.")
        morceaux.append(sortie)
        position += tentative
        cible = min(cible, max(0.15, tentative))
    return morceaux


async def debut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    context.user_data.pop("mode", None)
    context.user_data.pop("pending_url", None)
    await update.message.reply_text(
        "👋 أهلاً وسهلاً في بوت تحميل الفيديو والموسيقى!\n"
        "اختر ما تريد تحميله:\n\n"
        "🎬 فيديو: تحميل الفيديو مع الصوت\n"
        "🎵 موسيقى: استخراج الصوت بصيغة MP3\n\n"
        "📦 الحد الأقصى للتنزيل 150 ميغابايت. "
        "الملفات الكبيرة تُرسل على أجزاء.",
        reply_markup=clavier_choix(),
    )


async def choix_format(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()  # Important pour que les boutons Telegram cessent de charger.
    if query.data not in {"format:video", "format:audio"}:
        return
    mode = query.data.split(":", 1)[1]
    context.user_data["mode"] = mode
    nom = "🎬 فيديو" if mode == "video" else "🎵 موسيقى MP3"
    url = context.user_data.pop("pending_url", None)
    if not url:
        await query.message.reply_text(f"✅ اخترت {nom}. أرسل الآن رابط المقطع الذي تريد تنزيله.")
        return
    context.user_data.pop("mode", None)
    await executer(url, mode, query.message, query.from_user.id)


async def lien_recu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    url = extraire_url(message.text or "")
    if not url:
        await message.reply_text("🔗 أرسل رابط الفيديو من يوتيوب أو تيك توك أو X أو غيرها.",
                                 reply_markup=clavier_choix())
        return
    mode = context.user_data.pop("mode", None)
    if mode not in {"video", "audio"}:
        context.user_data["pending_url"] = url
        await message.reply_text("📥 وصل الرابط! كيف تريد تحميله؟", reply_markup=clavier_choix())
        return
    await executer(url, mode, message, update.effective_user.id)


async def executer(url: str, mode: str, message, user_id: int) -> None:
    if user_id in ACTIVE_USERS:
        await message.reply_text("⏳ لديك عملية تحميل قيد التنفيذ. انتظر حتى تنتهي.")
        return
    ACTIVE_USERS.add(user_id)
    etat = None
    try:
        etat = await message.reply_text("⏳ جارٍ التحميل…")
        async with JOBS:
            with tempfile.TemporaryDirectory(prefix="telegram_media_") as dossier:
                fichier, titre, duree = await asyncio.to_thread(telecharger_media, url, dossier, mode)
                if mode == "audio":
                    await etat.edit_text("🎵 جارٍ تحويل الصوت إلى MP3…")
                    fichier = await asyncio.to_thread(convertir_mp3, fichier, dossier)
                    duree = 0  # Recalculer la durée depuis le MP3 obtenu si nécessaire.
                await etat.edit_text("✂️ جارٍ تجهيز الملف للإرسال…")
                parties = await asyncio.to_thread(preparer_parties, fichier, duree, dossier, mode)
                nombre = len(parties)
                for i, partie in enumerate(parties, start=1):
                    await etat.edit_text(f"📤 جارٍ إرسال الجزء {i} من {nombre}…")
                    etiquette = f" | الجزء {i}/{nombre}" if nombre > 1 else ""
                    caption = f"{'🎵' if mode == 'audio' else '🎬'} {titre[:100]}{etiquette}"
                    with partie.open("rb") as flux:
                        if mode == "audio":
                            await message.reply_audio(
                                audio=flux, title=titre[:64], caption=caption,
                                read_timeout=240, write_timeout=240,
                            )
                        elif partie.suffix.lower() == ".mp4":
                            await message.reply_video(
                                video=flux, caption=caption, supports_streaming=True,
                                read_timeout=240, write_timeout=240,
                            )
                        else:
                            await message.reply_document(
                                document=flux, caption=caption,
                                read_timeout=240, write_timeout=240,
                            )
                await etat.edit_text("✅ تم الإرسال! أرسل رابطاً آخر أو اضغط /start لاختيار صيغة جديدة.")
    except Exception:
        logger.exception("Échec téléchargement/conversion/envoi")
        if etat:
            await etat.edit_text(
                "❌ تعذّر تحميل أو إرسال الملف. قد يكون الموقع يمنع التحميل، "
                "أو الملف أكبر من 150 ميغابايت، أو حدث خطأ في التحويل."
            )
        else:
            await message.reply_text("❌ حدث خطأ. حاول مرة أخرى.")
    finally:
        ACTIVE_USERS.discard(user_id)


def main() -> None:
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN غير موجود في إعدادات Render.")
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base_url.startswith("https://"):
        raise SystemExit("RENDER_EXTERNAL_URL غير موجود؛ استخدم Render Web Service.")
    secret = hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()[:40]
    route = f"telegram/{secret}"
    port = int(os.environ.get("PORT", "10000"))
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(8).build()
    app.add_handler(CommandHandler("start", debut))
    app.add_handler(CallbackQueryHandler(choix_format, pattern=r"^format:(video|audio)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lien_recu))
    logger.info("Démarrage sur le port %s (Telegram webhook).", port)
    app.run_webhook(
        listen="0.0.0.0", port=port, url_path=route,
        webhook_url=f"{base_url}/{route}", secret_token=secret,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
