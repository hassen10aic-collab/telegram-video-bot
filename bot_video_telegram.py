"""بوت تيليغرام لتنزيل الفيديوهات العامة حتى 150 ميغابايت.

مهم: Telegram Bot API الرسمية لا تسمح بإرسال ملف واحد أكبر من 50 ميغابايت.
لذلك يُقسم هذا البوت الفيديوهات الكبيرة إلى مقاطع MP4 مستقلة أصغر من 49 ميغابايت.
ضع TELEGRAM_BOT_TOKEN في متغيرات البيئة على Render فقط.
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
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("video_bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024  # أقصى حجم للفيديو الذي نحمله
MAX_SEND_BYTES = 47 * 1024 * 1024  # أقل من حد الـ 50MB في Telegram
MAX_PARTS = 20
VIDEO_JOBS = asyncio.Semaphore(2)


def telecharger_video(url: str, dossier: str) -> tuple[Path, str, float]:
    opts = {
        "format": "best[ext=mp4][height<=720]/best[ext=mp4]/best",
        "outtmpl": str(Path(dossier) / "video.%(ext)s"),
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
    candidats = [
        p for p in Path(dossier).glob("video.*")
        if p.is_file() and not p.name.endswith((".part", ".ytdl"))
    ]
    if not candidats:
        raise RuntimeError("لم يتم إنشاء ملف فيديو.")
    fichier = max(candidats, key=lambda p: p.stat().st_size)
    if fichier.stat().st_size > MAX_DOWNLOAD_BYTES:
        raise ValueError("حجم الفيديو يتجاوز 150 ميغابايت.")
    return fichier, str(info.get("title") or "فيديو"), float(info.get("duration") or 0)


def connaitre_duree(fichier: Path) -> float:
    """Lire la durée depuis FFmpeg lorsqu'elle n'est pas fournie par le site."""
    resultat = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(fichier)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    texte = resultat.stderr + resultat.stdout
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", texte)
    if not m:
        raise RuntimeError("تعذر معرفة مدة الفيديو لتقسيمه.")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def creer_morceau(fichier: Path, debut: float, longueur: float, sortie: Path) -> None:
    """Créer un clip MP4 autonome : le réencodage garantit une lecture normale."""
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
        "-nostdin", "-y", "-ss", f"{debut:.3f}", "-i", str(fichier),
        "-t", f"{longueur:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(sortie),
    ]
    resultat = subprocess.run(cmd, capture_output=True, text=True, timeout=240, check=False)
    if resultat.returncode != 0 or not sortie.exists() or sortie.stat().st_size < 1000:
        raise RuntimeError("FFmpeg: " + resultat.stderr[-1200:])


def decouper_video(fichier: Path, duree: float, dossier: str) -> list[Path]:
    """Découpe en clips MP4 lisibles de <47 MiB, avec taille contrôlée."""
    if fichier.stat().st_size <= MAX_SEND_BYTES:
        return [fichier]
    if duree <= 0:
        duree = connaitre_duree(fichier)
    if duree <= 0:
        raise RuntimeError("مدة الفيديو غير صالحة.")

    total = fichier.stat().st_size
    # Cibler environ 85% du maximum autorisé pour garder une marge.
    duree_cible = max(0.15, duree * MAX_SEND_BYTES * 0.85 / total)
    position = 0.0
    morceaux: list[Path] = []
    dossier_parts = Path(dossier) / "parties"
    dossier_parts.mkdir(exist_ok=True)

    while position < duree - 0.05:
        if len(morceaux) >= MAX_PARTS:
            raise RuntimeError("الفيديو يتطلب عدداً كبيراً جداً من الأجزاء.")
        reste = duree - position
        tentative = min(reste, duree_cible)
        sortie = dossier_parts / f"partie_{len(morceaux)+1:02d}.mp4"
        ok = False
        for essai in range(9):
            sortie.unlink(missing_ok=True)
            try:
                creer_morceau(fichier, position, tentative, sortie)
                taille = sortie.stat().st_size
                if taille <= MAX_SEND_BYTES:
                    ok = True
                    break
                tentative = max(0.15, tentative * min(0.8, MAX_SEND_BYTES / taille * 0.85))
            except (RuntimeError, subprocess.TimeoutExpired):
                tentative = max(0.15, tentative * 0.7)
        if not ok:
            raise RuntimeError("تعذر تقسيم الفيديو إلى مقاطع أقل من 49 ميغابايت.")
        morceaux.append(sortie)
        position += tentative
        # Utiliser la durée du dernier segment pour la prochaine estimation.
        duree_cible = min(duree_cible, max(0.15, tentative))
    return morceaux


async def debut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "👋 أهلاً وسهلاً! أرسل رابط فيديو عام مسموح لك بتنزيله، وسأحاول إرساله إليك.\n"
            "📦 الحد الأقصى: 150 ميغابايت للفيديو.\n"
            "✂️ إذا تجاوز الفيديو 49 ميغابايت فسيُرسل على شكل عدة مقاطع."
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
    async with VIDEO_JOBS:
        try:
            with tempfile.TemporaryDirectory(prefix="telegram_video_") as dossier:
                fichier, titre, duree = await asyncio.to_thread(telecharger_video, url, dossier)
                await statut.edit_text("✂️ جارٍ تجهيز الفيديو لإرساله…")
                morceaux = await asyncio.to_thread(decouper_video, fichier, duree, dossier)
                nombre = len(morceaux)
                for numero, morceau in enumerate(morceaux, start=1):
                    await statut.edit_text(f"📤 جارٍ إرسال الجزء {numero} من {nombre}…")
                    caption = f"🎬 {titre[:120]}" + (f" | الجزء {numero}/{nombre}" if nombre > 1 else "")
                    with morceau.open("rb") as flux:
                        if morceau.suffix.lower() == ".mp4":
                            await update.message.reply_video(
                                video=flux, caption=caption, supports_streaming=True,
                                read_timeout=240, write_timeout=240,
                            )
                        else:
                            await update.message.reply_document(
                                document=flux, caption=caption,
                                read_timeout=240, write_timeout=240,
                            )
                await statut.delete()
        except Exception:
            logger.exception("فشل تنزيل الفيديو أو تقسيمه أو إرساله")
            await statut.edit_text(
                "❌ تعذّر تنزيل أو إرسال الفيديو. ربما يمنع الموقع التنزيل، "
                "أو يزيد حجم الفيديو على 150 ميغابايت، أو حدث خطأ في تقسيمه."
            )


def main() -> None:
    if not TOKEN:
        raise SystemExit("متغير TELEGRAM_BOT_TOKEN غير موجود في Render.")
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base_url.startswith("https://"):
        raise SystemExit("متغير RENDER_EXTERNAL_URL غير موجود. يجب استخدام Web Service.")
    secret = hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()[:40]
    chemin = f"telegram/{secret}"
    port = int(os.environ.get("PORT", "10000"))
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", debut))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lien_recu))
    logger.info("Démarrage du bot Telegram sur le port %s (webhook).", port)
    app.run_webhook(
        listen="0.0.0.0", port=port, url_path=chemin,
        webhook_url=f"{base_url}/{chemin}",
        secret_token=secret, drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
