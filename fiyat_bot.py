# fiyat_bot.py
# Telegram Fiyat Alarm Botu – Hepsiburada/Trendyol/MediaMarkt TR için siteye özel seçiciler
# Komutlar:
#   /start
#   /track <URL> <hedef_fiyat>
#   /list
#   /stop

import os
import re
import json
import logging
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# -----------------------------
# AYARLAR
# -----------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHECK_MINUTES = int(os.getenv("KONTROL_DAKIKA", "10"))

# chat_id -> [{'url': str, 'target': Decimal, 'last_price': Decimal|None}]
WATCHES: dict[int, list[dict]] = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
REQ_KW = dict(headers={"User-Agent": UA}, timeout=20)

# -----------------------------
# Yardımcılar
# -----------------------------
def _to_decimal(s: str) -> Decimal | None:
    if not s:
        return None
    s = s.strip()
    # 5.499,00 -> 5499.00
    s = s.replace("\u00a0", " ").replace(".", "").replace(",", ".")
    # bazen "5499.00 TL" geliyor
    s = re.sub(r"[^\d\.]", "", s)
    try:
        val = Decimal(s)
        return val if val > 0 else None
    except InvalidOperation:
        return None

def _first_decimal(*vals) -> Decimal | None:
    for v in vals:
        d = _to_decimal(v) if isinstance(v, str) else None
        if d:
            return d
    return None

def _find_ldjson_prices(soup: BeautifulSoup) -> Decimal | None:
    """<script type="application/ld+json"> içinden offers.price okumaya çalış."""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or tag.text or "{}")
        except json.JSONDecodeError:
            # bazı sitelerde birden fazla JSON obje dizi halinde
            try:
                data = json.loads((tag.string or tag.text or "").strip().split("\n", 1)[0])
            except Exception:
                continue
        # tek obje veya liste
        candidates = []
        def collect(obj):
            if isinstance(obj, dict):
                # Product/offers.price
                off = obj.get("offers")
                if isinstance(off, dict):
                    candidates.append(off.get("price") or off.get("lowPrice") or off.get("highPrice"))
                # price belirtilmiş olabilir
                for k in ("price", "priceAmount"):
                    if k in obj:
                        candidates.append(obj[k])
                for v in obj.values():
                    collect(v)
            elif isinstance(obj, list):
                for it in obj:
                    collect(it)
        collect(data)
        for c in candidates:
            d = _to_decimal(str(c))
            if d:
                return d
    return None

# -----------------------------
# Siteye özel: Trendyol
# -----------------------------
def parse_trendyol(html: str, soup: BeautifulSoup) -> Decimal | None:
    # 1) ld+json
    d = _find_ldjson_prices(soup)
    if d:
        return d
    # 2) Sık görülen sınıflar
    for cls in ["prc-dsc", "prc-slg", "product-price", "product-price-container"]:
        el = soup.find(class_=cls)
        if el:
            d = _to_decimal(el.get_text(" ", strip=True))
            if d:
                return d
    # 3) meta itemprop="price"
    meta = soup.find(attrs={"itemprop": "price"})
    if meta and (meta.get("content") or meta.get("content") == "0"):
        d = _to_decimal(meta.get("content", ""))
        if d:
            return d
    # 4) Regex fallback
    m = re.search(r'"price"\s*:\s*"?(?P<p>[\d\.,]+)"?', html)
    if m:
        return _to_decimal(m.group("p"))
    return None

# -----------------------------
# Siteye özel: Hepsiburada
# -----------------------------
def parse_hepsiburada(html: str, soup: BeautifulSoup) -> Decimal | None:
    # 1) ld+json
    d = _find_ldjson_prices(soup)
    if d:
        return d
    # 2) meta property="product:price:amount"
    meta = soup.find("meta", attrs={"property": "product:price:amount"})
    if meta and meta.get("content"):
        d = _to_decimal(meta["content"])
        if d:
            return d
    # 3) Sık sınıflar
    classes = [
        "product-price", "price", "extra-discounted-price", "extra-discount-price"
    ]
    for cls in classes:
        el = soup.find(class_=cls)
        if el:
            d = _to_decimal(el.get_text(" ", strip=True))
            if d:
                return d
    # 4) window.__PRODUCT_DETAIL_APP_INITIAL_STATE__ JSON'undan çekme
    m = re.search(r"__PRODUCT_DETAIL_APP_INITIAL_STATE__\s*=\s*({.*?});", html, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(1))
            # olası yollar
            # obj["product"]["buybox"]["price"]["value"] gibi
            def deep_find(o, keys=("price", "value", "priceValue")):
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k in ("price", "value", "priceValue", "rawPrice", "sellingPrice"):
                            if isinstance(v, (int, float, str)):
                                d = _to_decimal(str(v))
                                if d:
                                    return d
                        if isinstance(v, (dict, list)):
                            r = deep_find(v, keys)
                            if r:
                                return r
                elif isinstance(o, list):
                    for it in o:
                        r = deep_find(it, keys)
                        if r:
                            return r
                return None
            d = deep_find(obj)
            if d:
                return d
        except Exception:
            pass
    # 5) Regex offers.price
    m = re.search(r'"offers"\s*:\s*{[^}]*"price"\s*:\s*"?(?P<p>[\d\.,]+)"?', html)
    if m:
        return _to_decimal(m.group("p"))
    return None

# -----------------------------
# Siteye özel: MediaMarkt TR
# -----------------------------
def parse_mediamarkt(html: str, soup: BeautifulSoup) -> Decimal | None:
    # 1) ld+json
    d = _find_ldjson_prices(soup)
    if d:
        return d
    # 2) meta property="product:price:amount"
    meta = soup.find("meta", attrs={"property": "product:price:amount"})
    if meta and meta.get("content"):
        d = _to_decimal(meta["content"])
        if d:
            return d
    # 3) Sık sınıflar (eski/yeni site sınıfları)
    classes = [
        "mm-u-price__sale-price", "big-price", "price", "price__integer-value", "pdp-price"
    ]
    for cls in classes:
        el = soup.find(class_=cls)
        if el:
            d = _to_decimal(el.get_text(" ", strip=True))
            if d:
                return d
    # 4) Regex
    m = re.search(r'"price"\s*:\s*"?(?P<p>[\d\.,]+)"?', html)
    if m:
        return _to_decimal(m.group("p"))
    return None

# -----------------------------
# Genel (fallback) yakalama
# -----------------------------
def parse_generic(html: str, soup: BeautifulSoup) -> Decimal | None:
    # 1) ld+json
    d = _find_ldjson_prices(soup)
    if d:
        return d
    # 2) ₺ / TL geçen metinler
    text = soup.get_text(" ", strip=True)
    patterns = [r"₺\s*([\d\.\,]+)", r"TL\s*([\d\.\,]+)"]
    cands = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            d = _to_decimal(m.group(1))
            if d:
                cands.append(d)
    if cands:
        cands.sort()
        return cands[0]
    return None

# -----------------------------
# Tek giriş: URL'ye göre uygun parser
# -----------------------------
def extract_price_from_page(url: str) -> Decimal | None:
    try:
        r = requests.get(url, **REQ_KW)
        r.raise_for_status()
    except requests.RequestException as e:
        logging.warning(f"GET hata: {e} | url={url}")
        return None

    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(url).netloc.lower()

    try:
        if "trendyol.com" in host:
            d = parse_trendyol(html, soup)
        elif "hepsiburada.com" in host:
            d = parse_hepsiburada(html, soup)
        elif "mediamarkt" in host and (".com.tr" in host or host.endswith("mediamarkt.com.tr")):
            d = parse_mediamarkt(html, soup)
        else:
            d = parse_generic(html, soup)
        return d
    except Exception as e:
        logging.warning(f"Parser hata: {e} | host={host}")
        return parse_generic(html, soup)

# -----------------------------
# JobQueue: Periyodik kontrol
# -----------------------------
async def periodic_check(context: ContextTypes.DEFAULT_TYPE):
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
                # alarm vereni listeden düş
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
        "Örn: /track https://www.trendyol.com/... 4999"
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
# Ana çalıştırma (senkron; PTB 21.x)
# -----------------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN çevre değişkeni eksik.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.job_queue.run_repeating(periodic_check, interval=CHECK_MINUTES * 60, first=10, name="fiyat_kontrol")
    logging.info("Bot çalışıyor... (polling başlıyor)")
    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Kapatıldı.")
