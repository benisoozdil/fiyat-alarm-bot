# fiyat_bot.py
# Telegram Fiyat Alarm Botu – her zaman açık sürüm
# Komutlar:
#   /start
#   /track <URL> <hedef_fiyat>   (örn: /track https://site.com/urun 4999)
#   /list
#   /stop

import os
import re
import logging
from decimal import Decimal, InvalidOperation

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# -----------------------------
# AYARLAR
# -----------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHECK_MINUTES = int(os.getenv("KONTROL_DAKIKA", "10"))

# chat_id -> [{'url': str, 'target': Decimal, 'last_price': Decimal|None}]
WATCHES: dict[int, list[dict]] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# -----------------------------
# Yardımcı: Sayfadan fiyat çek
# -----------------------------
def extract_price_from_page(url: str) -> Decimal | None:
    """
    Basit fiyat yakalama:
    - Sayfa metninde '₺' veya 'TL' geçen rakamları arar
    - Bulduğu adaylar içinden en küçük mantıklı değeri döndürür
    Not: Bazı sitelerde çalışmayabilir; siteye özel parser eklenebilir.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; FiyatBot/1.0; +https://example.com)"
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        logging.warning(f"GET hata: {e} | url={url}")
        return None

    text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)

    patterns = [
        r"₺\s*([\d\.\,]+)",
        r"TL\s*([\d\.\,]+)",
    ]

    candidates: list[Decimal] = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            raw = m.group(1)
            # 5.499,00 -> 5499.00
            norm = raw.replace(".", "").replace(",", ".")
            try:
                val = Decimal(norm)
                if val > 0:
                    candidates.append(val)
            except InvalidOperation:
                continue

    if not candidates:
        return None

    candidates.sort()
    return candidates[0]


# -----------------------------
# JobQueue: Periyodik kontrol
# -----------------------------
async def periodic_check(context: ContextTypes.DEFAULT_TYPE):
    """
    Her CHECK_MINUTES dakikada bir çağrılır.
    Hedefin altına düşenleri bildirir; alarm verenleri listeden çıkarır.
    """
    for chat_id, items in list(WATCHES.items()):
        new_list: list[dict] = []
        for it in items:
            url = it["url"]
            target: Decimal = it["target"]
            price = extract_price_from_page(url)
            it["last_price"] = price

            if price is not None and price <= target:
                msg = (
                    "🎉 Fiyat HEDEFİN ALTINDA!\n"
                    f"🔗 {url}\n"
                    f"💰 Şu an: {price} TL | 🎯 Hedef: {target} TL"
                )
                try:
                    await context.bot.send_message(chat_id=chat_id, text=msg)
                except Exception as e:
                    logging.warning(f"Bildirim hatası: {e}")
                # alarm veren ürünü listeden düş
            else:
                new_list.append(it)
        WATCHES[chat_id] = new_list


# -----------------------------
# Komutlar
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "Merhaba! Ben Fiyat Alarm Botu 🤖\n\n"
        "Komutlar:\n"
        "• /track <URL> <hedef_fiyat>\n"
        "• /list\n"
        "• /stop\n\n"
        "Örn: /track https://ornek.com/urun 4999"
    )
    await update.message.reply_text(txt)


async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Kullanım: /track <URL> <hedef_fiyat>")

    url = context.args[0].strip()
    target_str = context.args[1].replace(",", ".")
    try:
        target = Decimal(target_str)
        if target <= 0:
            raise InvalidOperation
    except InvalidOperation:
        return await update.message.reply_text("Hedef fiyat sayı olmalı. Örn: 4999")

    chat_id = update.effective_chat.id
    lst = WATCHES.get(chat_id, [])

    price = extract_price_from_page(url)
    lst.append({"url": url, "target": target, "last_price": price})
    WATCHES[chat_id] = lst

    reply = f"🔔 Takibe alındı:\n🔗 {url}\n🎯 Hedef: {target} TL"
    if price is not None:
        reply += f"\n📊 Şu an: {price} TL"
        if price <= target:
            reply += "\n🎉 Zaten hedefin altında!"
    else:
        reply += "\n⚠️ Şu an fiyatı bulamadım; periyodik olarak deneyeceğim."

    await update.message.reply_text(reply)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lst = WATCHES.get(chat_id, [])
    if not lst:
        return await update.message.reply_text("Takipte ürün yok.")

    lines = ["🔎 Takip Listesi:"]
    for i, it in enumerate(lst, 1):
        last = f"{it['last_price']} TL" if it["last_price"] is not None else "?"
        lines.append(f"{i}) {it['url']}\n   🎯 {it['target']} TL | Son: {last}")
    await update.message.reply_text("\n".join(lines))


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    WATCHES[chat_id] = []
    await update.message.reply_text("Bu sohbetteki tüm takipler durduruldu.")


# -----------------------------
# Ana çalıştırma (senkron)
# PTB 21.x: run_polling() senkron & bloklayıcı — asyncio.run() KULLANMIYORUZ.
# -----------------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN çevre değişkeni eksik.")

    app = Application.builder().token(BOT_TOKEN).build()

    # Komutlar
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    # Periyodik iş
    # job-queue eklentisi requirements'ta: python-telegram-bot[job-queue]==21.4
    app.job_queue.run_repeating(
        periodic_check,
        interval=CHECK_MINUTES * 60,
        first=10,
        name="fiyat_kontrol"
    )

    logging.info("Bot çalışıyor... (polling başlıyor)")
    app.run_polling()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Kapatıldı.")
