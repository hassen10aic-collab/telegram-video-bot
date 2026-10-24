"""Bot Telegram de téléchargement vidéo pour Render (webhook, sans PC allumé).

Déployer sur Render avec le fichier render.yaml et la variable secrète
TELEGRAM_BOT_TOKEN. Ne jamais publier son token sur GitHub.
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
        titre = (info or {}).get("title", "Vidéo")

    fichiers = [
        p for p in Path(dossier).glob("video.*")
        if p.is_file() and not p.name.endswith((".part", ".ytdl"))
    ]
    if not fichiers:
        raise RuntimeError("Aucun fichier vidéo n'a été créé.")
    return max(fichiers, key=lambda p: p.stat().st_size), str(titre)


async def debut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "👋 Envoie un lien de vidéo publique que tu as le droit de télécharger. "
            "Je tenterai de te renvoyer le fichier (49 Mo maximum)."
        )


async def lien_recu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    texte = update.message.text or ""
    correspondance = re.search(r"https?://[^\s<>]+", texte)
    if not correspondance:
        await update.message.reply_text("Envoie un lien de vidéo commençant par https://")
        return

    url = correspondance.group(0).rstrip(".,;)")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        await update.message.reply_text("Lien invalide.")
        return

    statut = await update.message.reply_text("⏳ Téléchargement en cours…")
    try:
        with tempfile.TemporaryDirectory(prefix="telegram_video_") as dossier:
            fichier, titre = await asyncio.to_thread(telecharger_video, url, dossier)
            if fichier.stat().st_size > MAX_BYTES:
                await statut.edit_text("❌ Vidéo trop volumineuse : limite 49 Mo.")
                return

            await statut.edit_text("📤 Envoi sur Telegram…")
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
        logger.exception("Échec du téléchargement ou de l'envoi")
        await statut.edit_text(
            "❌ Impossible de récupérer cette vidéo. Le site peut imposer une "
            "connexion, bloquer les téléchargements, ou le fichier est trop grand."
        )


def main() -> None:
    if not TOKEN:
        raise SystemExit("Variable TELEGRAM_BOT_TOKEN manquante sur Render.")

    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base_url.startswith("https://"):
        raise SystemExit("RENDER_EXTERNAL_URL manquant : déployer comme Web Service Render.")

    # Secret dérivé du token pour sécuriser le chemin et valider les requêtes Telegram.
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
