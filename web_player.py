"""
web_player.py - مشغل ويب محلي وبروكسي احترافي للبث التدفقي (Streaming Proxy)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
الوصف والمسؤولية:
  - استخراج روابط الفيديو وجوداتها من السيرفرات المختلفة.
  - تشغيل خادم بروكسي محلي متقدم يتعامل مع بروتوكولات HLS و MP4.
  - حقن ترويسات الحماية (Referer و User-Agent) تلقائياً لتخطي حظر 404 و 403.
  - دعم تدفق المقاطع (Streaming Chunks) عبر اتصالات مستمرة (Connection Pooling)
    دون تأخير المصافحة مع خوادم الـ CDN، مما يضمن تحميل الفيديو بالكامل دون توقف.
  - دعم كامل لطلبات CORS Preflight (OPTIONS) وطلب الأجزاء (HTTP Range / 206 Partial Content).
  - إعادة كتابة قوائم التشغيل (m3u8) وحل جميع الروابط النسبية ومفاتيح التشفير بدقة.

المستدعي والتبعيات:
  - يستورد video_extractor_ALL.py لاستخراج الروابط.
  - يُستدعى مباشرة عبر: python web_player.py
  - يفتح المتصفح تلقائياً على http://127.0.0.1:8001
"""

import base64
import json
import os
import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import video_extractor_ALL as core

# ─── الإعدادات العامة ────────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 8001))
_URI_PAT = re.compile(r'URI="([^"]+)"')

# ─── مجمع الاتصالات المتكررة (Persistent Connection Pool) ─────────────────────
# استخدام Session موحدة مع Connection Pooling يمنع إعادة مصافحة TLS/SSL
# مع كل مقطع .ts (كل 2-4 ثوانٍ)، مما يسرع تدفق البيانات ويمنع تقطع البث.
_session = requests.Session()
_retries = Retry(
    total=3,
    backoff_factor=0.3,
    status_forcelist=[500, 502, 503, 504],
    raise_on_status=False,
)
_adapter = HTTPAdapter(
    pool_connections=50,
    pool_maxsize=50,
    max_retries=_retries,
)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)


def safe_b64encode(s: str) -> str:
    """تشفير آمن للروابط في عناوين URL مع إزالة الرموز الإشكالية."""
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def safe_b64decode(s: str) -> str:
    """فك تشفير آمن مع تعويض علامات الحشو (Padding) التلقائية."""
    if not s:
        return ""
    clean = s.replace("-", "+").replace("_", "/")
    padded = clean + "=" * ((4 - len(clean) % 4) % 4)
    try:
        return base64.b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def collect_links() -> list[dict]:
    """استخراج جميع الروابط المتاحة وتجهيزها في هيكل بيانات موحد."""
    items = []
    for name, embed in core.SERVERS:
        entry = {"server": name, "embed": embed, "playable": []}
        try:
            if "streamtape" in name.lower():
                ref = embed.split("/e/")[0]
                for r in core.extract_streamtape(embed):
                    entry["playable"].append({
                        "kind": r["kind"],
                        "label": r.get("label", "1080p"),
                        "res": r.get("res", "1080p"),
                        "url": r["url"],
                        "ref": ref,
                    })
            elif "vidmoly" in name.lower():
                ref = embed.split("/embed-")[0] + "/"
                for r in core.extract_vidmoly(embed):
                    entry["playable"].append({
                        "kind": r["kind"],
                        "label": r.get("label", "720p"),
                        "res": r.get("res", "720p"),
                        "url": r["url"],
                        "ref": ref,
                    })
            else:
                master = core.unpack_master_url(embed)
                if not master:
                    items.append(entry)
                    continue

                ref = embed
                txt = core.fetch_master(master, ref)
                if not txt:
                    ref = ""
                    txt = core.fetch_master(master, ref)

                if txt:
                    for v in core.parse_variants(txt, master):
                        entry["playable"].append({
                            "kind": "HLS",
                            "label": v["label"],
                            "res": v["res"],
                            "url": v["url"],
                            "ref": ref,
                        })
        except Exception:
            pass

        items.append(entry)
    return items


def rewrite_playlist(text: str, playlist_url: str, referer: str) -> str:
    """
    إعادة كتابة قائمة تشغيل HLS (m3u8):
    تحويل مسارات المقاطع ومفاتيح التشفير لتعبر من خلال البروكسي،
    مع حل كافة المسارات النسبية لتصبح روابط كاملة ومطلقة.
    """
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue

        # سطور عناوين المقاطع (Segments) أو القوائم الفرعية (Variant Playlists)
        if not s.startswith("#"):
            resolved_url = s if s.startswith("http") else urljoin(playlist_url, s)
            proxy_url = f"/proxy?u={safe_b64encode(resolved_url)}&r={safe_b64encode(referer)}"
            out.append(proxy_url)
        # وسوم مفاتيح التشفير والخرائط الأولية (Encryption Keys & Init Maps)
        elif s.startswith("#EXT-X-KEY:") or s.startswith("#EXT-X-MAP:"):
            def _replace_uri(m):
                raw_uri = m.group(1)
                full_uri = raw_uri if raw_uri.startswith("http") else urljoin(playlist_url, raw_uri)
                return f'URI="/proxy?u={safe_b64encode(full_uri)}&r={safe_b64encode(referer)}"'
            out.append(_URI_PAT.sub(_replace_uri, line))
        else:
            out.append(line)

    return "\n".join(out)


class StreamingProxyHandler(BaseHTTPRequestHandler):
    """معالج طلبات الويب والبروكسي المتدفق مع دعم كامل للـ CORS والـ Range."""

    def log_message(self, format, *args):
        # تعطيل السجلات الافتراضية لمنع إغراق الطرفية بطلبات المقاطع اللحظية
        pass

    def _send_cors_headers(self):
        """إرسال ترويسات مشاركة الموارد (CORS) لكافة المتصفحات والمشغلات."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type, Authorization, Accept, X-Requested-With")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")

    def do_OPTIONS(self):
        """التعامل مع طلبات التحقق المسبق (Preflight Requests)."""
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_error(self, code: int, message: str):
        body = message.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _proxy_stream(self, target_url: str, referer: str):
        """
        جلب وتدفق محتوى الفيديو أو المقاطع من الخادم الأصلي مع حقن الترويسات
        ودعم الاستجابة الجزئية (206 Partial Content) لتقديم وتأخير الفيديو بسلاسة.
        """
        req_headers = dict(core.UA)
        if referer:
            req_headers["Referer"] = referer

        # تمرير ترويسة المدى إذا طلب المشغل جزءاً معيناً من الفيديو
        if "Range" in self.headers:
            req_headers["Range"] = self.headers["Range"]

        try:
            upstream_resp = _session.get(
                target_url,
                headers=req_headers,
                stream=True,
                timeout=35,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            return self._send_error(502, f"Proxy Gateway Error: {str(e)}")

        # إذا كانت الاستجابة غير ناجحة
        if upstream_resp.status_code >= 400:
            status = upstream_resp.status_code
            upstream_resp.close()
            return self._send_error(status, f"Upstream returned {status}")

        content_type = (upstream_resp.headers.get("Content-Type") or "").lower().split(";")[0].strip()

        # التحقق: هل الملف قائمة تشغيل HLS (m3u8)؟
        # يتم التحقق إما عبر الامتداد أو ترويسة النوع أو فحص البداية لقوائم النصوص
        is_hls_url = target_url.endswith(".m3u8") or ".m3u8?" in target_url or target_url.endswith(".txt")
        is_playlist_type = content_type in ("application/vnd.apple.mpegurl", "application/x-mpegurl", "text/plain")

        if is_hls_url or is_playlist_type:
            # قراءة ومعالجة قائمة التشغيل وإعادة كتابة الروابط للمرور عبر البروكسي
            raw_text = upstream_resp.text
            upstream_resp.close()
            if raw_text.lstrip().startswith("#EXTM3U"):
                modified_playlist = rewrite_playlist(raw_text, target_url, referer)
                body_bytes = modified_playlist.encode("utf-8")

                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl; charset=utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(body_bytes)
                return

        # تدفق البيانات المباشر لمقاطع .ts وملفات .mp4
        self.send_response(upstream_resp.status_code)
        if content_type:
            self.send_header("Content-Type", content_type)
        else:
            self.send_header("Content-Type", "video/mp2t" if target_url.endswith(".ts") else "application/octet-stream")

        self._send_cors_headers()

        # نقل الترويسات الهامة لدعم الـ Range و Seeking
        for header_key in ("Content-Length", "Content-Range", "Accept-Ranges"):
            if header_key in upstream_resp.headers:
                self.send_header(header_key, upstream_resp.headers[header_key])

        self.end_headers()

        # تدفق المقاطع مباشرة بكتل 64KB لمنع احتجاز الذاكرة
        try:
            for chunk in upstream_resp.iter_content(chunk_size=65536):
                if chunk:
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            upstream_resp.close()

    def do_GET(self):
        parsed = urlparse(self.path)

        # 1. الصفحة الرئيسية
        if parsed.path == "/":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        # 2. واجهة استخراج الروابط (API)
        if parsed.path == "/api/links":
            links = collect_links()
            body = json.dumps(links, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        # 3. نقطة تدفق البروكسي (Proxy Endpoint)
        if parsed.path == "/proxy":
            params = parse_qs(parsed.query)
            target_url = safe_b64decode(params.get("u", [""])[0])
            referer = safe_b64decode(params.get("r", [""])[0])

            if not target_url.startswith("http://") and not target_url.startswith("https://"):
                return self._send_error(400, "Invalid Target URL")

            return self._proxy_stream(target_url, referer)

        self._send_error(404, "Not Found")


# ─── واجهة المشغل الحديثة ───────────────────────────────────────────────────
HTML_PAGE = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>مشغل الفيديو الاحترافي — البث المباشر المكتمل</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
<style>
  :root {
    --bg: #0b0f19;
    --card: #151c2c;
    --card-border: #242f48;
    --txt: #f1f5f9;
    --sub: #94a3b8;
    --primary: #38bdf8;
    --primary-glow: rgba(56, 189, 248, 0.25);
    --ok: #10b981;
    --err: #ef4444;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--txt);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
    padding: 24px;
    max-width: 1000px;
    margin: 0 auto;
  }
  header { margin-bottom: 20px; }
  h1 { font-size: 22px; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 10px; }
  .badge { background: var(--primary-glow); color: var(--primary); font-size: 12px; padding: 3px 8px; border-radius: 6px; }
  .desc { font-size: 14px; color: var(--sub); margin-top: 6px; }
  
  #player-container {
    background: #000;
    border: 1px solid var(--card-border);
    border-radius: 12px;
    overflow: hidden;
    margin-bottom: 24px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.5);
  }
  video {
    width: 100%;
    max-height: 480px;
    display: none;
    background: #000;
  }
  #player-status {
    padding: 12px 16px;
    background: var(--card);
    font-size: 13px;
    color: var(--primary);
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-top: 1px solid var(--card-border);
  }
  .buffer-info { color: var(--sub); font-size: 12px; }

  .server-card {
    background: var(--card);
    border: 1px solid var(--card-border);
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 12px;
    transition: transform 0.15s ease, border-color 0.15s ease;
  }
  .server-card:hover { border-color: #3b82f6; }
  .server-header { display: flex; justify-content: space-between; align-items: center; }
  .server-title { font-weight: 600; font-size: 15px; display: flex; align-items: center; gap: 8px; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; }
  .status-ok { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
  .status-fail { background: var(--err); }
  .server-url { color: var(--sub); font-size: 12px; direction: ltr; text-align: left; max-width: 450px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .quality-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .chip {
    background: #1e293b;
    border: 1px solid #334155;
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 13px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 6px;
    transition: all 0.2s;
  }
  .chip:hover {
    background: #2563eb;
    border-color: #3b82f6;
    transform: translateY(-1px);
  }
  .chip-active {
    background: var(--primary);
    color: #000;
    border-color: var(--primary);
    font-weight: 700;
  }
  .chip-type { font-size: 10px; opacity: 0.7; }
</style>
</head>
<body>

<header>
  <h1>مشغل الفيديو التدفقي <span class="badge">PRO STREAM PROXY</span></h1>
  <p class="desc">البروكسي يقوم بحقن الترويسات وتدفق المقاطع المتتابعة بصورة متصلة لضمان تحميل ومشاهدة الفيديو بالكامل دون توقف.</p>
</header>

<div id="player-container">
  <video id="video-element" controls playsinline></video>
  <div id="player-status">
    <span id="current-stream">جاهز للاختيار والتشغيل</span>
    <span id="buffer-state" class="buffer-info"></span>
  </div>
</div>

<div id="servers-list">جارٍ فحص واستخراج الروابط من السيرفرات...</div>

<script>
const enc = s => encodeURIComponent(btoa(unescape(encodeURIComponent(s || ''))).replace(/=/g, ''));
const makeProxyUrl = (url, ref) => '/proxy?u=' + enc(url) + '&r=' + enc(ref);
const $ = id => document.getElementById(id);

let hlsInstance = null;

function stopCurrentPlayer() {
  if (hlsInstance) {
    try { hlsInstance.destroy(); } catch(e){}
    hlsInstance = null;
  }
  const v = $('video-element');
  v.pause();
  v.removeAttribute('src');
  v.load();
}

function startPlayback(item, chipEl) {
  stopCurrentPlayer();
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('chip-active'));
  if (chipEl) chipEl.classList.add('chip-active');

  const v = $('video-element');
  v.style.display = 'block';
  $('current-stream').textContent = 'جارٍ البث: ' + item.server + ' [' + item.label + ']';

  const proxyStreamUrl = makeProxyUrl(item.url, item.ref);

  if (item.kind === 'MP4') {
    v.src = proxyStreamUrl;
    v.play().catch(() => {});
    return;
  }

  // إعدادات HLS الاحترافية المتقدمة لتدفق دائم دون توقف مؤقت
  if (window.Hls && Hls.isSupported()) {
    hlsInstance = new Hls({
      maxBufferLength: 120,             // الحفاظ على تخزين مؤقت متقدم دقيقتين للأمام
      maxMaxBufferLength: 600,          // السماح بالتخزين حتى 10 دقائق
      maxBufferSize: 120 * 1024 * 1024, // ذاكرة تخزين مؤقت حتى 120 ميجابايت
      enableWorker: true,               // فك التشفير في مسار معالج مستقل (Web Worker)
      lowLatencyMode: false,            // وضع VOD لتدفق موثوق ومستقر
      backBufferLength: 60              // الحفاظ على دقيقة خلفية للرجوع للخلف بسرعة
    });

    hlsInstance.loadSource(proxyStreamUrl);
    hlsInstance.attachMedia(v);

    hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
      v.play().catch(() => {});
    });

    // استعادة ذاتية للأخطاء عند بطء الشبكة
    hlsInstance.on(Hls.Events.ERROR, (event, data) => {
      if (data.fatal) {
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
          $('buffer-state').textContent = 'استعادة اتصال الشبكة...';
          hlsInstance.startLoad();
        } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
          $('buffer-state').textContent = 'معالجة وسائط البث...';
          hlsInstance.recoverMediaError();
        } else {
          stopCurrentPlayer();
          $('current-stream').textContent = 'تعذر تشغيل هذا المسار: ' + (data.details || 'خطأ غير معروف');
        }
      }
    });

    // متابعة حالة التخزين المؤقت الحية
    setInterval(() => {
      if (v.buffered.length > 0) {
        const bufferedEnd = v.buffered.end(v.buffered.length - 1);
        const duration = v.duration || 0;
        const current = v.currentTime;
        const bufferedSeconds = Math.max(0, bufferedEnd - current);
        $('buffer-state').textContent = 'المخزن مؤقتاً للأمام: ' + Math.round(bufferedSeconds) + ' ثانية';
      }
    }, 1000);

  } else if (v.canPlayType('application/vnd.apple.mpegurl')) {
    // دعم أصلي لأجهزة iOS Safari و Mac
    v.src = proxyStreamUrl;
    v.play().catch(() => {});
  } else {
    $('current-stream').textContent = 'المتصفح لا يدعم بث HLS';
  }
}

function renderServerList(data) {
  const container = $('servers-list');
  container.innerHTML = '';

  data.forEach(srv => {
    const card = document.createElement('div');
    card.className = 'server-card';

    const header = document.createElement('div');
    header.className = 'server-header';

    const hasLinks = srv.playable && srv.playable.length > 0;
    header.innerHTML = `
      <div class="server-title">
        <span class="status-dot ${hasLinks ? 'status-ok' : 'status-fail'}"></span>
        ${srv.server}
      </div>
      <div class="server-url">${srv.embed}</div>
    `;
    card.appendChild(header);

    if (hasLinks) {
      const chips = document.createElement('div');
      chips.className = 'quality-chips';
      srv.playable.forEach(p => {
        const chip = document.createElement('div');
        chip.className = 'chip';
        chip.innerHTML = `<span>${p.label}</span> <span class="chip-type">${p.kind} ${p.res || ''}</span>`;
        chip.onclick = () => startPlayback(p, chip);
        chips.appendChild(chip);
      });
      card.appendChild(chips);
    } else {
      const noMsg = document.createElement('div');
      noMsg.style.color = 'var(--sub)';
      noMsg.style.fontSize = '12px';
      noMsg.style.marginTop = '8px';
      noMsg.textContent = 'لا توجد روابط قابلة للتشغيل من هذا السيرفر';
      card.appendChild(noMsg);
    }

    container.appendChild(card);
  });
}

fetch('/api/links')
  .then(res => res.json())
  .then(renderServerList)
  .catch(err => {
    $('servers-list').textContent = 'حدث خطأ أثناء تحميل الروابط: ' + err;
  });
</script>

</body>
</html>
"""


def main():
    """تشغيل الخادم واستقبال الاتصالات العامة أو المحلية."""
    server = ThreadingHTTPServer(("0.0.0.0", PORT), StreamingProxyHandler)
    print("=" * 65)
    print("  مشغل الفيديو الاحترافي والبث المتدفق يعمل الآن")
    print(f"  المنفذ: {PORT}")
    print("=" * 65)
    # فتح المتصفح فقط في بيئة التطوير المحلية
    if not os.environ.get("RENDER") and not os.environ.get("PORT"):
        try:
            webbrowser.open(f"http://127.0.0.1:{PORT}")
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nتم إيقاف الخادم.")


if __name__ == "__main__":
    main()