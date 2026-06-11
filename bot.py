import os
import logging
import base64
import asyncio
from io import BytesIO

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    filters,
    ConversationHandler,
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
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

# Conversation states
WAITING_ID = 1
WAITING_DIPLOMA = 2

# Geçici veri: {user_id: {"id_file_id": ..., "diploma_file_id": ...}}
pending_data: dict = {}


async def download_photo_as_base64(bot, file_id: str) -> str:
    file = await bot.get_file(file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


async def analyze_with_gemini(id_b64: str, diploma_b64: str) -> dict:
    prompt = """Sana iki fotoğraf gönderiyorum:
1. Birinci fotoğraf: Türkiye Cumhuriyeti kimlik kartı
2. İkinci fotoğraf: Üniversite diploması

Lütfen şunları kontrol et:
- Kimlik kartındaki AD SOYAD
- Diplomadaki AD SOYAD
- Diplomadaki BÖLÜM ADI (matematik bölümü mü?)
- Kimlik ve diplomadaki adların birbiriyle UYUŞUP UYUŞMADIĞI

Cevabını SADECE şu JSON formatında ver, başka hiçbir şey yazma:
{
  "kimlik_ad_soyad": "...",
  "diploma_ad_soyad": "...",
  "diploma_bolum": "...",
  "matematik_bolumu": true/false,
  "ad_uyusumu": true/false,
  "notlar": "varsa ek notlar, yoksa boş bırak"
}"""

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": id_b64,
                        }
                    },
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": diploma_b64,
                        }
                    },
                ]
            }
        ]
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(GEMINI_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()

    raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
    # JSON bloğunu temizle
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
    raw_text = raw_text.strip()

    import json
    return json.loads(raw_text)


# --- Join Request Handler ---
async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.chat_join_request.from_user
    user_id = user.id

    pending_data[user_id] = {}

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
                "📌 Bilgileriniz yalnızca kimlik doğrulama amacıyla kullanılacaktır. Bu yüzden sadece kimlikteki ad soayad ve diplomadaki ad soyad ile bölüm dışındaki yerleri karalayınız.\n\n"
                "Lütfen önce *kimlik kartı fotoğrafınızı* gönderin:"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"Kullanıcıya DM gönderilemedi: {user_id} - {e}")
        return

    context.user_data["awaiting"] = WAITING_ID


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if user_id not in pending_data:
        return

    photo = update.message.photo[-1]  # En yüksek çözünürlük
    file_id = photo.file_id
    state = pending_data[user_id]

    if "id_file_id" not in state:
        # Kimlik fotoğrafı alındı
        state["id_file_id"] = file_id
        await update.message.reply_text(
            "✅ Kimlik fotoğrafı alındı.\n\nŞimdi *diploma fotoğrafınızı* gönderin:",
            parse_mode="Markdown",
        )

    elif "diploma_file_id" not in state:
        # Diploma fotoğrafı alındı
        state["diploma_file_id"] = file_id
        await update.message.reply_text(
            "✅ Diploma fotoğrafı alındı. Bilgileriniz inceleniyor, lütfen bekleyin... 🔍"
        )

        # Analiz başlat
        await process_application(update, context, user)


async def process_application(update, context, user):
    user_id = user.id
    state = pending_data.get(user_id, {})

    id_file_id = state.get("id_file_id")
    diploma_file_id = state.get("diploma_file_id")

    if not id_file_id or not diploma_file_id:
        return

    try:
        # Fotoğrafları indir
        id_b64 = await download_photo_as_base64(context.bot, id_file_id)
        diploma_b64 = await download_photo_as_base64(context.bot, diploma_file_id)

        # Gemini ile analiz et
        result = await analyze_with_gemini(id_b64, diploma_b64)

        # Arşiv kanalına gönder
        caption = (
            f"📋 YENİ BAŞVURU\n"
            f"👤 Kullanıcı: {user.full_name} (@{user.username or 'yok'}) [ID: {user_id}]\n"
            f"─────────────────\n"
            f"🪪 Kimlik Adı: {result.get('kimlik_ad_soyad', '?')}\n"
            f"🎓 Diploma Adı: {result.get('diploma_ad_soyad', '?')}\n"
            f"📚 Bölüm: {result.get('diploma_bolum', '?')}\n"
            f"─────────────────\n"
            f"{'✅' if result.get('matematik_bolumu') else '❌'} Matematik Bölümü\n"
            f"{'✅' if result.get('ad_uyusumu') else '❌'} Ad Uyuşumu\n"
        )
        if result.get("notlar"):
            caption += f"📝 Not: {result['notlar']}\n"

        # Kimlik fotoğrafı arşive
        await context.bot.send_photo(
            chat_id=ARCHIVE_CHANNEL_ID,
            photo=id_file_id,
            caption=f"🪪 KİMLİK — {user.full_name} [ID: {user_id}]",
        )
        # Diploma fotoğrafı arşive
        await context.bot.send_photo(
            chat_id=ARCHIVE_CHANNEL_ID,
            photo=diploma_file_id,
            caption=caption,
        )

        # Admin'e bildirim + butonlar
        matematik_ok = result.get("matematik_bolumu", False)
        ad_ok = result.get("ad_uyusumu", False)

        if matematik_ok and ad_ok:
            oneri = "✅ Otomatik öneri: ONAYLA"
        else:
            sorunlar = []
            if not matematik_ok:
                sorunlar.append("matematik bölümü değil")
            if not ad_ok:
                sorunlar.append("ad uyuşmuyor")
            oneri = f"⚠️ Sorun: {', '.join(sorunlar)}"

        admin_text = (
            f"📨 YENİ ÜYELİK BAŞVURUSU\n\n"
            f"👤 {user.full_name} (@{user.username or 'yok'})\n"
            f"🪪 Kimlik: {result.get('kimlik_ad_soyad', '?')}\n"
            f"🎓 Diploma: {result.get('diploma_ad_soyad', '?')}\n"
            f"📚 Bölüm: {result.get('diploma_bolum', '?')}\n\n"
            f"{'✅' if matematik_ok else '❌'} Matematik bölümü\n"
            f"{'✅' if ad_ok else '❌'} Ad uyuşumu\n\n"
            f"{oneri}"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Onayla", callback_data=f"approve_{user_id}"),
                InlineKeyboardButton("❌ Reddet", callback_data=f"decline_{user_id}"),
            ]
        ])

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_text,
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error(f"Analiz hatası: {e}")

        # State'i sıfırla, kullanıcıdan tekrar iste
        pending_data[user_id] = {}
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "⚠️ Belgeleriniz işlenirken teknik bir sorun oluştu, özür dileriz.\n\n"
                "Lütfen tekrar deneyin. Önce *kimlik kartı fotoğrafınızı* gönderin:"
            ),
            parse_mode="Markdown",
        )

        # Admin'e sadece bilgi ver
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"⚠️ Analiz hatası — kullanıcıdan tekrar istendi.\n"
                f"Kullanıcı: {user.full_name} [ID: {user_id}]\n"
                f"Hata: {e}"
            ),
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
        await query.edit_message_text(
            query.message.text + "\n\n✅ ONAYLANDI"
        )
        await context.bot.send_message(
            chat_id=user_id,
            text="🎉 Başvurunuz onaylandı! Gruba hoş geldiniz.",
        )
    elif action == "decline":
        await context.bot.decline_chat_join_request(
            chat_id=TARGET_GROUP_ID, user_id=user_id
        )
        await query.edit_message_text(
            query.message.text + "\n\n❌ REDDEDİLDİ"
        )
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "Üzgünüz, başvurunuz onaylanamadı.\n"
                "Sorun olduğunu düşünüyorsanız grup yöneticisiyle iletişime geçebilirsiniz."
            ),
        )

    # Temizle
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
