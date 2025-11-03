
# Fiyat Alarm Botu (Telegram) – Her zaman açık sürüm

Bu proje, Telegram'da fiyat izleme botunu **daima açık** şekilde çalıştırmak için hazır bir pakettir.

## Hızlı Başlangıç (Railway / Render / VPS)

1) Bu klasörü bir Git deposu yapın ve GitHub'a gönderin.
2) Platformda **Background Worker / Service** oluşturun (web portu gerektirmeyen tür).
3) Ortam değişkenlerini ekleyin:
   - `TELEGRAM_BOT_TOKEN` → BotFather tokenınız
   - (Opsiyonel) `KONTROL_DAKIKA` → Varsayılan: 10
4) Başlat komutu: `python fiyat_bot.py`

### Docker ile
```
docker build -t fiyat-bot .
docker run -e TELEGRAM_BOT_TOKEN=XXX -e KONTROL_DAKIKA=10 fiyat-bot
```

## Yerelde çalıştırma
```
pip install -r requirements.txt
set TELEGRAM_BOT_TOKEN=XXX   (Windows)
export TELEGRAM_BOT_TOKEN=XXX (macOS/Linux)
python fiyat_bot.py
```

## Komutlar
- `/start`
- `/track <URL> <hedef_fiyat>`
- `/list`
- `/stop`

> Not: Fiyat çıkarımı basit tutulmuştur; kimi sitelerde özel seçici/parsing eklemek gerekebilir.
