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
            "👋 أهلاً بك! أرسل رابط فيديو عام مسموح لك بتنزيله، وسأحاول إرسال الفيديو إليك. "
            "📦 الحد الأقصى لحجم الملف: 49 ميغابايت."
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

    statut = await update.message.reply_text("⏳ جاري تنزيل الفيديو…")
    try:
        with tempfile.TemporaryDirectory(prefix="telegram_video_") as dossier:
            fichier, titre = await asyncio.to_thread(telecharger_video, url, dossier)
            if fichier.stat().st_size > MAX_BYTES:
                await statut.edit_text("❌ حجم الفيديو كبير جداً. الحد الأقصى 49 ميغابايت.")
                return

            await statut.edit_text("📤 جاري إرسال الفيديو عبر تيليغرام…")
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
            "❌ تعذّر تنزيل هذا الفيديو. قد يطلب الموقع تسجيل الدخول، أو يمنع التنزيل، "
            "أو قد يكون حجم الملف أكبر من المسموح."
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
