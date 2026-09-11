import os
import json
import logging
import tempfile

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    BotCommand,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN est manquant.")

# CHANNELS doit être une variable d'environnement au format JSON, exemple :
# [{"name":"Mon Canal Principal","url":"https://t.me/moncanal","id":"@moncanal"},
#  {"name":"Mon Canal Backup","url":"https://t.me/moncanal2","id":"@moncanal2"}]
CHANNELS = json.loads(os.getenv("CHANNELS", "[]"))

WELCOME_TEXT = os.getenv(
    "WELCOME_TEXT",
    "👋 Bienvenue !\n\n"
    "Pour utiliser ce bot, rejoins d'abord les canaux ci-dessous, "
    "puis clique sur ✅ J'ai rejoint."
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Stockage temporaire en mémoire : {user_id: {"thumb": path, "video": path, "tmp_dir": path}}
user_sessions = {}


# ============================================================
# VERIFICATION D'ABONNEMENT AUX CANAUX
# ============================================================

async def get_missing_channels(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    missing = []
    for channel in CHANNELS:
        try:
            member = await context.bot.get_chat_member(channel["id"], user_id)
            if member.status in (
                ChatMemberStatus.LEFT,
                ChatMemberStatus.BANNED,
            ):
                missing.append(channel)
        except Exception:
            # Si le bot n'est pas admin du canal ou erreur API, on considère non-abonné
            missing.append(channel)
    return missing


def build_join_keyboard(missing_channels):
    rows = [
        [InlineKeyboardButton(f"📢 {c['name']}", url=c["url"])]
        for c in missing_channels
    ]
    rows.append([InlineKeyboardButton("✅ J'ai rejoint", callback_data="check_join")])
    return InlineKeyboardMarkup(rows)


async def require_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Retourne True si l'utilisateur peut continuer, False sinon (message envoyé)."""
    user_id = update.effective_user.id
    missing = await get_missing_channels(user_id, context)

    if not missing:
        return True

    text = WELCOME_TEXT
    keyboard = build_join_keyboard(missing)

    if update.callback_query:
        await update.callback_query.answer("❌ Tu n'as pas encore rejoint tous les canaux.", show_alert=True)
        try:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard)
        except Exception:
            pass
    else:
        await update.message.reply_text(text, reply_markup=keyboard)

    return False


async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    missing = await get_missing_channels(user_id, context)

    if missing:
        await query.answer("❌ Il te manque encore des canaux à rejoindre.", show_alert=True)
        await query.edit_message_text(WELCOME_TEXT, reply_markup=build_join_keyboard(missing))
        return

    await query.answer("✅ Accès débloqué !")
    await query.edit_message_text(
        "✅ Merci de nous avoir rejoint !\n\n"
        "Envoie-moi maintenant une photo qui servira de miniature, "
        "puis envoie la vidéo juste après.",
    )


# ============================================================
# COMMANDES
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, context):
        return

    await update.message.reply_text(
        "✅ Salut ! Voici comment m'utiliser :\n\n"
        "1️⃣ Envoie-moi une photo (elle servira de miniature)\n"
        "2️⃣ Envoie-moi ensuite la vidéo\n"
        "3️⃣ Choisis si tu veux la recevoir en 🎬 Vidéo ou 📄 Document\n\n"
        "Utilise /help pour revoir ces instructions, /cancel pour recommencer."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, context):
        return

    await update.message.reply_text(
        "📚 AIDE\n\n"
        "/start — Démarrer le bot\n"
        "/help — Afficher cette aide\n"
        "/cancel — Annuler la session en cours\n\n"
        "📸 Envoie une photo, puis 🎬 une vidéo pour recevoir "
        "la vidéo avec ta miniature personnalisée."
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cleanup_session(user_id)
    await update.message.reply_text("❌ Session réinitialisée. Envoie une nouvelle photo pour recommencer.")


def cleanup_session(user_id: int):
    session = user_sessions.pop(user_id, None)
    if session:
        for key in ("thumb", "video"):
            path = session.get(key)
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


# ============================================================
# RECEPTION PHOTO (MINIATURE)
# ============================================================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, context):
        return

    user_id = update.effective_user.id
    cleanup_session(user_id)  # on repart sur une nouvelle session à chaque nouvelle photo

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    tmp_dir = tempfile.mkdtemp(prefix=f"thumb_{user_id}_")
    thumb_path = os.path.join(tmp_dir, "thumb.jpg")
    await file.download_to_drive(thumb_path)

    user_sessions[user_id] = {"thumb": thumb_path, "video": None, "tmp_dir": tmp_dir}

    await update.message.reply_text(
        "🖼️ Miniature enregistrée !\n\nEnvoie-moi maintenant la vidéo."
    )


# ============================================================
# RECEPTION VIDEO / DOCUMENT
# ============================================================

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, context):
        return

    user_id = update.effective_user.id
    session = user_sessions.get(user_id)

    if not session or not session.get("thumb"):
        await update.message.reply_text(
            "⚠️ Envoie d'abord une photo pour définir la miniature, puis renvoie la vidéo."
        )
        return

    media = update.message.video or update.message.document
    if media is None:
        await update.message.reply_text("❌ Merci d'envoyer une vidéo ou un fichier vidéo.")
        return

    status = await update.message.reply_text("⏳ Téléchargement de la vidéo...")

    file = await context.bot.get_file(media.file_id)
    video_path = os.path.join(session["tmp_dir"], "video.mp4")
    await file.download_to_drive(video_path)

    session["video"] = video_path

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Envoyer en vidéo", callback_data="send_video"),
            InlineKeyboardButton("📄 Envoyer en document", callback_data="send_document"),
        ]
    ])

    await status.edit_text("✅ Vidéo reçue ! Comment veux-tu la recevoir ?", reply_markup=keyboard)


# ============================================================
# ENVOI FINAL AVEC MINIATURE
# ============================================================

async def send_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    session = user_sessions.get(user_id)

    if not session or not session.get("video") or not session.get("thumb"):
        await query.edit_message_text("❌ Session expirée. Recommence en envoyant une photo.")
        return

    await query.edit_message_text("📤 Envoi en cours...")

    video_path = session["video"]
    thumb_path = session["thumb"]

    try:
        with open(video_path, "rb") as video_file, open(thumb_path, "rb") as thumb_file:
            if query.data == "send_video":
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=InputFile(video_file),
                    thumbnail=InputFile(thumb_file),
                    supports_streaming=True,
                )
            else:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=InputFile(video_file),
                    thumbnail=InputFile(thumb_file),
                )

        await query.edit_message_text("✅ Terminé ! Envoie une nouvelle photo pour recommencer.")

    except Exception as e:
        logger.exception("Erreur lors de l'envoi")
        await query.edit_message_text(f"❌ Erreur lors de l'envoi : {e}")

    finally:
        cleanup_session(user_id)


# ============================================================
# MENU DE COMMANDES (visible via le bouton menu de Telegram)
# ============================================================

async def setup_commands(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Démarrer le bot"),
        BotCommand("help", "Afficher l'aide"),
        BotCommand("cancel", "Annuler la session en cours"),
    ])


# ============================================================
# APPLICATION
# ============================================================

def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(setup_commands)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    application.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    application.add_handler(CallbackQueryHandler(send_final, pattern="^send_(video|document)$"))

    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))

    logger.info("Bot démarré, en attente de messages...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
  
