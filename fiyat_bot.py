# fiyat_bot.py
# Sürekli çalışan Telegram Fiyat Alarm Botu (polling)
# Komutlar:
# /start
# /track <URL> <hedef_fiyat>
# /list
# /stop

import re
import asyncio
import logging
import requests
from bs4 import BeautifulSoup
from decimal import Decimal, InvalidOperation
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import os

# ---- AYARLAR ----
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHECK_MINUTES = int(os.getenv("KONTROL_DAKIKA", "10"))

# Bellekte takip listesi: chat_id -> [{'url':..., 'target': Decimal, 'last_price': Decimal|None}]
WATCHES = {}

logging.basicConfig(level=logging.INFO)

def extract_price_from_page(url: str):
    """Basit fiyat yakalama: sayfada '₺' veya 'TL' içeren rakamları arar."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FiyatBot/1.0)"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return None

    text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
    patterns = [r"₺\s*([\d\.\,]+)", r"TL\s*([\d\.\,]+)"]
    candidates = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            raw = m.group(1).replace(".", "").replace(",", ".")
            try:
                val = Decimal(raw)
                if val > 0:
                    candidates.append(val)
            except InvalidOperation:
                pass
    if not candidates:
        return None
    candidates.sort()
    return candidates[0]

async def periodic_check(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, items in list(WATCHES.items()):
        new_list = []
        for it in items:
            url = it["url"]
            target = it["target"]
            price = extract_price_from_page(url)
            it["last_price"] = price
            if price is not None and price <= target:
                msg = (f"🎉 Fiyat hedefin ALTINDA!\n🔗 {url}\n💰 Şu an: {price} TL | 🎯 Hedef: {target} TL")
                try:
                    await context.bot.send_message(chat_id=chat_id, text=msg)
                except Exception as e:
                    logging.warning(f"Bildirim hatası: {e}")
            else:
                new_list.append(it)
        WATCHES[chat_id] = new_list

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

async def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN çevre değişkeni eksik.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.job_queue.run_repeating(periodic_check, interval=CHECK_MINUTES * 60, first=10)
    await app.run_polling(close_loop=False)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Kapatıldı.")
