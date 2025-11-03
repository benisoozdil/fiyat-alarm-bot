# fiyat_bot.py
# Telegram Fiyat Alarm Botu – siteye özel seçiciler + kuruş (minor units) düzeltmesi
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
    s = s.strip().replace("\u00a0", " ")
    # 25.000,00 TL -> "25000.00"
    s = s.replace(".", "").replace(",", ".")
    # son temizlik: sadece rakam ve nokta
    s = re.sub(r"[^\d\.]", "", s)
    if not s:
        return None
    try:
        val = Decimal(s)
        return val if val > 0 else None
    except InvalidOperation:
        return None

def _fix_minor_units(val: Decimal | None) -> Decimal | None:
    """
    Birçok sitede JSON sayısal fiyatlar kuruş (minor units) olarak gelebilir:
      25.000 TL -> 2.500.000 (kuruş)
    Heuristik:
      - Değer çok büyükse (>= 100000) ve 100'e tam bölünüyorsa -> /100
    """
    if val is None:
        return None
    try:
        if val >= 100_000 and (val % 100 == 0):
            adj = val / Decimal(100)
            # Aşırı düzeltmeyi engelle: 100 milyondan büyükse yine saçma olabilir
            if adj < 100_000_000:
                logging.info(f"[minor-fix] {val} -> {adj}")
                return adj
    except Exception:
        pass
    return val

def _pick_best(cands: list[Decimal]) -> Decimal | None:
    """Adayları filtrele ve mantıklı en iyi fiyatı seç."""
    if not cands:
        return None
    # Kuruş düzeltmesini uygula
    cands = [_fix_minor_units(x) or x for x in cands]
    # Negatif/0 dışarı
    cands = [x for x in cands if x and x > 0]
    if not cands:
        return None
    # Çok absürt büyük değerleri (medyana göre 10x üstü) ele
    cands_sorted = sorted(cands)
    mid = cands_sorted[len(cands_sorted)//2]
    sane = [x for x in cands_sorted if x <= (mid * 10)]
    if not sane:
        sane = cands_sorted
    # Genelde gerçek fiyat küçük adaylar arasında olur (kargo/ufak rakamlar hariç)
    # 100 TL altı (kargo vs.) çok küçükse ele
    sane2 = [x for x in sane if x >= 100]
    base = sane2 if sane2 else sane
    return sorted(base)[0]

def _find_ldjson_prices(soup: BeautifulSoup) -> Decimal | None:
    """<script type="application/ld+json"> içinden offers.price vb. çek."""
    cands: list[Decimal] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.text or ""
        if not raw.strip():
            continue
        # Bazı sitelerde dizi, bazılarında tek obje; bazen de birden çok JSON satırı var
        # Kolay yol: JSON parse etmeyi deney, olmazsa satır satır dene
        tries = [raw]
        if "\n" in raw:
            tries.extend([line.strip() for line in raw.splitlines() if line.strip().startswith("{")])
        for payload in tries:
            try:
                data = json.loads(payload)
            except Exception:
                continue
            # iç içe her yerde price/lowPrice/highPrice/priceAmount ara
            def collect(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k in ("price", "lowPrice", "highPrice", "priceAmount", "priceValue", "rawPrice", "sellingPrice"):
                            if isinstance(v, (int, float, str)):
                                d = _to_decimal(str(v)) if isinstance(v, str) else Decimal(str(v))
                                d = _fix_minor_units(d)
                                if d:
                                    cands.append(d)
                        if isinstance(v, (dict, list)):
                            collect(v)
                elif isinstance(obj, list):
                    for it in obj:
                        collect(it)
            collect(data)
    return _pick_best(cands)

# -----------------------------
# Siteye özel: Trendyol
# -----------------------------
def parse_trendyol(html: str, soup: BeautifulSoup) -> Decimal | None:
    d = _find_ldjson_prices(soup)
    if d:
        return d
    # Sık sınıflar
    for cls in ["prc-dsc", "prc-slg", "product-price", "product-price-container"]:
        el = soup.find(class_=cls)
        if el:
            d = _to_decimal(el.get_text(" ", strip=True))
            d = _fix_minor_units(d)
            if d:
                return d
    # itemprop="price"
    meta = soup.find(attrs={"itemprop": "price"})
    if meta and (meta.get("content") or meta.get("content") == "0"):
        d = _to_decimal(meta.get("content", ""))
        d = _fix_minor_units(d)
        if d:
            return d
    # JSON fallback
    m = re.search(r'"price"\s*:\s*"?(?P<p>[\d\.,]+)"?', html)
    if m:
        d = _to_decimal(m.group("p"))
        return _fix_minor_units(d)
    return None

# -----------------------------
# Siteye özel: Hepsiburada
# -----------------------------
def parse_hepsiburada(html: str, soup: BeautifulSoup) -> Decimal | None:
    d = _find_ldjson_prices(soup)
    if d:
        return d
    meta = soup.find("meta", attrs={"property": "product:price:amount"})
    if meta and meta.get("content"):
        d = _to_decimal(meta["content"])
        d = _fix_minor_units(d)
        if d:
            return d
    for cls in ["product-price", "price", "extra-discounted-price", "extra-discount-price"]:
        el = soup.find(class_=cls)
        if el:
            d = _to_decimal(el.get_text(" ", strip=True))
            d = _fix_minor_units(d)
            if d:
                return d
    # Hepsiburada sayfa state JSON
    m = re.search(r"__PRODUCT_DETAIL_APP_INITIAL_STATE__\s*=\s*({.*?});", html, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(1))
            cands = []
            def deep(o):
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k in ("price", "value", "priceValue", "rawPrice", "sellingPrice"):
                            if isinstance(v, (int, float, str)):
                                d = _to_decimal(str(v)) if isinstance(v, str) else Decimal(str(v))
                                d = _fix_minor_units(d)
                                if d:
                                    cands.append(d)
                        if isinstance(v, (dict, list)):
                            deep(v)
                elif isinstance(o, list):
                    for it in o:
                        deep(it)
            deep(obj)
            best = _pick_best(cands)
            if best:
                return best
        except Exception:
            pass
    m = re.search(r'"offers"\s*:\s*{[^}]*"price"\s*:\s*"?(?P<p>[\d\.,]+)"?', html)
    if m:
        d = _to_decimal(m.group("p"))
        return _fix_minor_units(d)
    return None

# -----------------------------
# Siteye özel: MediaMarkt TR
# -----------------------------
def parse_mediamarkt(html: str, soup: BeautifulSoup) -> Decimal | None:
    d = _find_ldjson_prices(soup)
    if d:
        return d
    meta = soup.find("meta", attrs={"property": "product:price:amount"})
    if meta and meta.get("content"):
        d = _to_decimal(meta["content"])
        d = _fix_minor_units(d)
        if d:
            return d
    for cls in ["mm-u-price__sale-price", "big-price", "price", "price__integer-value", "pdp-price"]:
        el = soup.find(class_=cls)
        if el:
            d = _to_decimal(el.get_text(" ", strip=True))
            d = _fix_minor_units(d)
            if d:
                return d
    m = re.search(r'"price"\s*:\s*"?(?P<p>[\d\.,]+)"?', html)
    if m:
        d = _to_decimal(m.group("p"))
        return _fix_minor_units(d)
    return None

# -----------------------------
# Genel (fallback)
# -----------------------------
def parse_generic(html: str, soup: BeautifulSoup) -> Decimal | None:
    d = _find_ldjson_prices(soup)
    if d:
        return d
    text = soup.get_text(" ", strip=True)
    cands = []
    for pat in (r"₺\s*([\d\.\,]+)", r"TL\s*([\d\.\,]+)"):
        for m in re.finditer(pat, text):
            d = _to_decimal(m.group(1))
            d = _fix_minor_units(d)
            if d:
                cands.append(d)
    return _pick_best(cands)

# -----------------------------
# Tek giriş
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
