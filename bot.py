import os
import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
TARGET_GROUP_ID = int(os.environ["TARGET_GROUP_ID"])
ARCHIVE_CHANNEL_ID = int(os.environ["ARCHIVE_CHANNEL_ID"])
GROUP_LINK = "https://t.me/+imWCy38bbsdjOTI0"

REMINDER_TEXT = (
    "⏰ *Hatırlatma:* Kimlik kartı ve diploma fotoğraflarınızı henüz göndermediniz.\n\n"
    "Yalnızca belgelerinizdeki *ad soyad* ve *bölüm* bilgisi kontrol edilmektedir.\n\n"
    "⚠️ İlgili belgeleri göndermediğiniz takdirde gruba gönderdiğiniz katılım isteği reddedilecektir.\n\n"
    "Bot mesajını geç gördüyseniz veya tekrar istek göndermek istiyorsanız grup linkimiz aşağıda:\n"
    f"{GROUP_LINK}"
)

# Hatırlatma süreleri (saniye)
REMINDER_DELAYS = [
    15 * 60,       # 1. hatırlatma: 15 dakika
    60 * 60,       # 2. hatırlatma: 1 saat
    6 * 60 * 60,   # 3. hatırlatma: 6 saat
]
AUTO_DECLINE_DELAY = 2 * 60 * 60  # 3. hatırlatmadan 2 saat sonra otomatik red

# pending_data[user_id] = {
#   "id_file_id": str,
#   "diploma_file_id": str,
#   "reminder_count": int,
#   "full_name": str,
#   "username": str,
# }
pending_data: dict = {}


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.data["user_id"]
    reminder_num = job.data["reminder_num"]  # 1, 2, 3

    state = pending_data.get(user_id)

    # Kullanıcı zaten belgelerini gönderdiyse iptal et
    if not state or ("id_file_id" in state and "diploma_file_id" in state):
        return

    if reminder_num <= 2:
        # Hatırlatma gönder
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=REMINDER_TEXT,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"Hatırlatma gönderilemedi {user_id}: {e}")

        # Sonraki hatırlatmayı planla
        next_reminder = reminder_num + 1
        delay = REMINDER_DELAYS[reminder_num]  # index 1 → 2. gecikme, index 2 → 3. gecikme
        context.job_queue.run_once(
            send_reminder,
            when=delay,
            data={"user_id": user_id, "reminder_num": next_reminder},
            name=f"reminder_{user_id}_{next_reminder}",
        )

    elif reminder_num == 3:
        # Son hatırlatma — 2 saat sonra reddedileceğini bildir
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    REMINDER_TEXT + "\n\n"
                    "🔴 Bu son hatırlatmadır. *2 saat içinde* belgelerinizi göndermezseniz "
                    "katılım isteğiniz otomatik olarak reddedilecektir."
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"Son hatırlatma gönderilemedi {user_id}: {e}")

        # 2 saat sonra otomatik red
        context.job_queue.run_once(
            auto_decline,
            when=AUTO_DECLINE_DELAY,
            data={"user_id": user_id},
            name=f"decline_{user_id}",
        )


async def auto_decline(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.data["user_id"]

    state = pending_data.get(user_id)

    # Belgeler geldiyse iptal et
    if not state or ("id_file_id" in state and "diploma_file_id" in state):
        return

    try:
        await context.bot.decline_chat_join_request(
            chat_id=TARGET_GROUP_ID, user_id=user_id
        )
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Belgeler gönderilmediği için katılım isteğiniz reddedilmiştir.",
        )
        full_name = state.get("full_name", "Bilinmiyor")
        username = state.get("username", "yok")
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"🚫 Otomatik reddedildi (belge gönderilmedi)\n"
                f"👤 {full_name} (@{username}) [ID: {user_id}]"
            ),
        )
    except Exception as e:
        logger.warning(f"Otomatik red başarısız {user_id}: {e}")

    pending_data.pop(user_id, None)


def cancel_reminders(job_queue, user_id: int):
    for i in range(1, 4):
        jobs = job_queue.get_jobs_by_name(f"reminder_{user_id}_{i}")
        for job in jobs:
            job.schedule_removal()
    # Otomatik red job'ını da iptal et
    for job in job_queue.get_jobs_by_name(f"decline_{user_id}"):
        job.schedule_removal()


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.chat_join_request.from_user
    user_id = user.id

    pending_data[user_id] = {
        "full_name": user.full_name,
        "username": user.username or "yok",
    }

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"Merhaba {user.first_name}! 👋\n\n"
                "Matematik Öğretmenleri grubuna katılmak için başvurunuzu aldık.\n\n"
                "Üyelik onayı için lütfen sırasıyla:\n"
                "1️⃣ TC Kimlik kartınızın fotoğrafını\n"
                "2️⃣ Üniversite diplomasının fotoğrafını\n\n"
                "gönderin.\n\n"
                "📌 Yalnızca belgelerinizdeki *ad soyad* ve *bölüm* bilgisi kontrol edilmektedir. Diğer bilgileri karalayınız !! \n\n"
                "Lütfen önce *kimlik kartı fotoğrafınızı* gönderin:"
            ),
            parse_mode="Markdown",
        )

        # İlk hatırlatmayı planla (15 dakika sonra)
        context.job_queue.run_once(
            send_reminder,
            when=REMINDER_DELAYS[0],
            data={"user_id": user_id, "reminder_num": 1},
            name=f"reminder_{user_id}_1",
        )

    except Exception as e:
        logger.warning(f"Kullanıcıya DM gönderilemedi: {user_id} - {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if user_id not in pending_data:
        return

    photo = update.message.photo[-1]
    file_id = photo.file_id
    state = pending_data[user_id]

    if "id_file_id" not in state:
        state["id_file_id"] = file_id
        await update.message.reply_text(
            "✅ Kimlik fotoğrafı alındı.\n\nŞimdi *diploma fotoğrafınızı* gönderin:",
            parse_mode="Markdown",
        )

    elif "diploma_file_id" not in state:
        state["diploma_file_id"] = file_id

        # Hatırlatmaları iptal et
        cancel_reminders(context.job_queue, user_id)

        await update.message.reply_text(
            "✅ Diploma fotoğrafı alındı. Başvurunuz incelemeye alındı, en kısa sürede bilgilendirileceksiniz. 🔍"
        )
        await process_application(context, user)


async def process_application(context, user):
    user_id = user.id
    state = pending_data.get(user_id, {})

    id_file_id = state.get("id_file_id")
    diploma_file_id = state.get("diploma_file_id")

    if not id_file_id or not diploma_file_id:
        return

    try:
        # Arşiv kanalına gönder
        await context.bot.send_photo(
            chat_id=ARCHIVE_CHANNEL_ID,
            photo=id_file_id,
            caption=f"🪪 KİMLİK — {user.full_name} (@{user.username or 'yok'}) [ID: {user_id}]",
        )
        await context.bot.send_photo(
            chat_id=ARCHIVE_CHANNEL_ID,
            photo=diploma_file_id,
            caption=f"🎓 DİPLOMA — {user.full_name} (@{user.username or 'yok'}) [ID: {user_id}]",
        )

        # Admin'e fotoğrafları gönder
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=id_file_id,
            caption=f"🪪 KİMLİK\n👤 {user.full_name} (@{user.username or 'yok'})",
        )
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=diploma_file_id,
            caption=f"🎓 DİPLOMA\n👤 {user.full_name} (@{user.username or 'yok'})",
        )

        # Onay/red butonları
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Onayla", callback_data=f"approve_{user_id}"),
                InlineKeyboardButton("❌ Reddet", callback_data=f"decline_{user_id}"),
            ]
        ])

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"📨 YENİ ÜYELİK BAŞVURUSU\n\n"
                f"👤 {user.full_name}\n"
                f"🔗 @{user.username or 'yok'}\n"
                f"🆔 {user_id}\n\n"
                f"Belgeleri inceleyip karar verin:"
            ),
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error(f"Hata: {e}")
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ Hata oluştu!\nKullanıcı: {user.full_name} [ID: {user_id}]\nHata: {e}",
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("Bu butonu kullanma yetkiniz yok.", show_alert=True)
        return

    data = query.data
    action, user_id_str = data.split("_", 1)
    user_id = int(user_id_str)

    if action == "approve":
        await context.bot.approve_chat_join_request(
            chat_id=TARGET_GROUP_ID, user_id=user_id
        )
        await query.edit_message_text(query.message.text + "\n\n✅ ONAYLANDI")
        await context.bot.send_message(
            chat_id=user_id,
            text="🎉 Başvurunuz onaylandı! Gruba hoş geldiniz.",
        )
    elif action == "decline":
        await context.bot.decline_chat_join_request(
            chat_id=TARGET_GROUP_ID, user_id=user_id
        )
        await query.edit_message_text(query.message.text + "\n\n❌ REDDEDİLDİ")
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "Üzgünüz, başvurunuz onaylanamadı.\n"
                "Sorun olduğunu düşünüyorsanız grup yöneticisiyle iletişime geçebilirsiniz."
            ),
        )

    cancel_reminders(context.job_queue, user_id)
    pending_data.pop(user_id, None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Merhaba! Bu bot Matematik Öğretmenleri grubu üyelik onay botudur."
    )


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot başlatılıyor...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
