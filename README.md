# Video Extractor & Streaming Proxy Service

خادم ويب عام لاستخراج روابط الفيديو الحقيقية (بجودات متعددة من 360p إلى 1080p) وتشغيلها عبر بروكسي تدفقي فائق السرعة.

## المميزات
- استخراج وفك تشفير الجودات لـ 7 سيرفرات رئيسية:
  - StreamTape (MP4 1080p)
  - VidMoly (HLS 720p)
  - Hlswish (HLS 720p)
  - Uqload (HLS 360p + 720p)
  - Vidoba (HLS 360p + 480p)
  - VidSpeed (HLS 360p + 480p)
  - 1Vid (HLS 720p)
- مشغل فيديو مدمج في واجهة الويب يدعم HLS و MP4.
- بروكسي تدفقي ذكي لحقن ترويسات الحماية (Referer) وتدفق المقاطع دون انقطاع عبر Persistent Connection Pooling.
- متوافق بالكامل مع خوادم Render و Cloudflare.
