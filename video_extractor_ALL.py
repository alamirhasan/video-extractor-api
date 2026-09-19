"""
VideoTube Link Extractor - ALL
سكربت واحد يشغّل جميع السيرفرات ويعرض كل الجودات المتاحة لكل فيديو.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
التشغيل:  python video_extractor_ALL.py
"""

import json
import re
import sys
from urllib.parse import urljoin

import quickjs
import requests

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

# ─── قائمة كل السيرفرات (اسم، رابط الإمبيد) ─────────────────────────────
SERVERS = [
    ("StreamTape", "https://streamtape.cc/e/x21rykKlejfq8Y"),
    ("VidMoly",    "https://vidmoly.org/embed-604x2134gp7y.html"),
    ("Hlswish",    "https://hlswish.com/e/g736i9sbr1yp"),
    ("Uqload",     "https://uqload.vc/embed-ygjp5jsvwbpa.html"),
    ("Vidoba",     "https://vidoba.org/embed-qtpujelr7loz.html"),
    ("VidSpeed",   "https://vidspeed.org/embed-7k83jkg6gf8f.html"),
    ("1Vid",       "https://1vid.xyz/embed-47hf2idiv1tt.html"),
    ("VideoTube",  "https://down.vidtube.one/embed-azg9z3mggmva.html"),
    ("UpDown",     "https://updown.icu/embed-vnqad84khy96-1280x640.html"),
    ("StreamWish", "https://streamwish.fun/e/xm9odld6uodl"),
    ("Doodstream", "https://d0o0d.com/e/ik0y3fff3fxn"),
    ("Filelions",  "https://earnvids.xyz/v/98izp7m4wkdw"),
    ("Bysesukior", "https://bysesukior.com/e/b7glp4wbrssi/The.Wrong.Girls.2026.1080p.WEB-DL.mp4"),
    ("VOE",        "https://voe.sx/e/dkrwla141xwe"),
    ("DsvPlay",    "https://dsvplay.com/e/v2zelcl1k051"),
]


def find_packer_blocks(html: str) -> list[str]:
    blocks = []
    search_from = 0
    tpl = "eval(function(p,a,c,k,e,d)"
    while True:
        start = html.find(tpl, search_from)
        if start < 0:
            break
        open_pos = html.find("(", start)
        depth = 0
        i = open_pos
        while i < len(html):
            c = html[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth == 0:
            blocks.append(html[start:i + 1])
            search_from = i + 1
        else:
            search_from = start + 10
    return blocks


STUB_ENV = r"""
var __captured = null;
var window = this;
window.location = { href: location_href };
window.screen = {};
window._ = {};
var navigator = { userAgent: 'Mozilla/5.0', appName: 'Netscape', language: 'en' };
var location = window.location;
var document = {
  getElementById: function(){ return null; },
  createElement: function(){ return { style: {}, setAttribute: function(){}, appendChild: function(){}, addEventListener: function(){}, set src(v){}, get src(){ return ''; } }; },
  getElementsByTagName: function(){ return []; },
  querySelector: function(){ return null; },
  querySelectorAll: function(){ return []; },
  addEventListener: function(){}, cookie: '', referrer: '',
  body: { appendChild: function(){} },
  head: { appendChild: function(){} },
};
var Regex = RegExp;
function jwplayer(key){ return {
  setup: function(cfg){ __captured = cfg; },
  on: function(){ return { on: function(){} }; },
  el: function(){ return { style: {} }; },
  addButton: function(){}, getState: function(){ return 'idle'; },
}; }
function videojs(){ return {
  ready: function(fn){ fn(); }, src: function(){}, play: function(){}, on: function(){},
  currentTime: function(){ return 0; }, validateSubtypes: function(){}, el: function(){ return { style:{} }; },
}; }
function $(){ return { cookie: function(){}, ajax: function(){}, get: function(){}, post: function(){} }; }
$.ajax = function(){}; $.get = function(){}; $.post = function(){}; $.cookie = function(){}; $.getScript = function(){};
$.fn = { ready: function(fn){ fn(); } };
function gtag(){}
function ym(){}
var lastError = null;
"""


def unpack_master_url(embed_url: str) -> str | None:
    """فكّ الصفحة واستخراج عنوان master.m3u8"""
    try:
        r = requests.get(embed_url, headers=UA, timeout=40)
        html = r.text
    except requests.RequestException:
        return None
    blocks = find_packer_blocks(html)
    if not blocks:
        return None
    ctx = quickjs.Context()
    ctx.eval(f"var location_href = {json.dumps(embed_url)};")
    ctx.eval(STUB_ENV)
    for blk in blocks:
        ctx.eval(f"try {{ {blk}; }} catch(e) {{ lastError = String(e); }}")
    s = ctx.eval("__captured ? JSON.stringify(__captured) : 'null'")
    if s == "null":
        return None
    try:
        cfg = json.loads(s)
    except Exception:
        return None
    srcs = cfg.get("sources") or []
    for x in srcs:
        u = x.get("file") or x.get("src")
        if u and ("m3u8" in u.lower() or "master" in u.lower() or ".txt" in u.lower()):
            return u
    for k in ("file", "source"):
        if k in cfg:
            return cfg[k]
    return None


def fetch_master(url: str, referer: str) -> str | None:
    h = dict(UA)
    if referer:
        h["Referer"] = referer
    try:
        r = requests.get(url, headers=h, timeout=30)
        return r.text if r.status_code == 200 else None
    except requests.RequestException:
        return None


def std_quality(w: int, h: int) -> str:
    """ يحوّل الأبعاد إلى أقرب تسمية قياسية معتمدة على العرض:
        3840 -> 4K, 2560 -> 1440p, 1920 -> 1080p, 1280 -> 720p,
        854  -> 480p, 640 -> 360p, 426 -> 240p, الباقي -> 144p
        (1280x692 = 720p، 664x360 = 360p، 888x480 = 480p) """
    if w >= 3200:
        return "4K"
    if w >= 2560:
        return "1440p"
    if w >= 1920:
        return "1080p"
    if w >= 1280:
        return "720p"
    if w >= 854:
        return "480p"
    if w >= 640:
        return "360p"
    if w >= 426:
        return "240p"
    return "144p"


def parse_variants(text: str, base: str) -> list[dict]:
    out = []
    RES = re.compile(r"RESOLUTION=(\d+)x(\d+)")
    BAND = re.compile(r"BANDWIDTH=(\d+)")
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("#EXT-X-STREAM-INF:"):
            res_m = RES.search(ln)
            band_m = BAND.search(ln)
            i += 1
            if i < len(lines):
                uri = lines[i].strip()
                url = uri if uri.startswith("http") else requests.compat.urljoin(base, uri)
                if res_m:
                    w, h = int(res_m.group(1)), int(res_m.group(2))
                    out.append({
                        "res": f"{res_m.group(1)}x{res_m.group(2)}",
                        "label": std_quality(w, h),
                        "band": int(band_m.group(1)) if band_m else 0,
                        "url": url,
                    })
        i += 1
    out.sort(key=lambda v: v["label"])
    return out


def probe(url: str, referer: str) -> str:
    h = dict(UA)
    if referer:
        h["Referer"] = referer
    try:
        r = requests.head(url, headers=h, timeout=20, allow_redirects=True)
        return str(r.status_code)
    except requests.RequestException:
        return "ERR"


# ─── التحقق: هل الرابط فيديو؟ هل يعمل؟ سليم أم تالف؟ ─────────────────────

def magic_name(data: bytes) -> str:
    """يتعرّف على نوع الملف من أول بايتات (التوقيع السحري)"""
    if not data:
        return "empty"
    if b"ftyp" in data[:64]:          # ملف MP4 / fMP4
        return "MP4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "WebM"
    if data[:4] == b"\x4f\x67\x67\x53":
        return "Ogg"
    if len(data) >= 376 and data[0] == 0x47 and data[188] == 0x47 and data[376] == 0x47:
        return "MPEG-TS"
    if data[:7] == b"#EXTM3U":
        return "HLS"
    if b"<html" in data[:1024].lower() or b"<!doctype" in data[:1024].lower():
        return "PageHTML"
    return "Unknown"


def probe_bytes(url: str, referer: str):
    """يجلب أول كيلوبايت من الملف (لا يحمّل الكل) لفحص رأسه"""
    h = dict(UA)
    if referer:
        h["Referer"] = referer
    h["Range"] = "bytes=0-895"
    try:
        r = requests.get(url, headers=h, timeout=25, stream=True)
        data = b""
        if r.status_code in (200, 206):
            for chunk in r.iter_content(4096):
                data += chunk
                if len(data) >= 1024:
                    break
        ctype = (r.headers.get("Content-Type") or "").split(";")[0]
        clen = r.headers.get("Content-Length")
        return r.status_code, data, ctype, clen
    except requests.RequestException as e:
        return "ERR", b"", "", str(e)[:60]


def verify_file(url: str, referer: str) -> str:
    """يستعلم: هل هذا ملف فيديو مباشر سليم؟"""
    st, data, ctype, clen = probe_bytes(url, referer)
    if st == "ERR":
        return "✗ لا يمكن الوصول"
    if st in (403, 404, 410):
        return f"✗ ميت (HTTP {st})"
    if st not in (200, 206):
        return f"✗ HTTP {st}"
    if not data:
        return "✗ فارغ (بايتات 0)"
    mag = magic_name(data)
    if mag == "MP4" and len(data) > 64:
        return "✓ فيديو سليم (MP4)"
    if mag == "WebM":
        return "✓ فيديو سليم (WebM)"
    if mag == "MPEG-TS":
        return "✓ فيديو سليم (TS)"
    if mag == "PageHTML":
        return "✗ ليس فيديو (استجاب صفحة HTML)"
    return f"? مريب ({mag})"


def verify_hls(url: str, referer: str) -> str:
    """يستعلم: هل قائمة HLS سليمة وفيها مقاطع قابلة للتشغيل؟"""
    txt = fetch_master(url, referer)
    if txt is None:
        return "✗ ميت (لا يمكن جلب القائمة)"
    if not txt.startswith("#EXTM3U"):
        return "✗ ليست قائمة HLS صالحة"
    segs = re.findall(r"#EXTINF[^\r\n]*\r?\n([^\r\n]+)", txt)
    if not segs:
        return "✗ قائمة فارغة (بلا مقاطع)"
    first = segs[0].strip()
    if not first.startswith("http"):
        first = requests.compat.urljoin(url, first)
    st, data, ctype, clen = probe_bytes(first, referer)
    if st in (403, 404, 410):
        return f"✗ المقاطع مرفوضة (HTTP {st})"
    mag = magic_name(data)
    total = len(segs)
    if mag in ("MP4", "MPEG-TS"):
        return f"✓ يعمل (HLS: {total} مقطع، أول مقطع {mag} سليم)"
    return f"? القائمة سليمة لكن المقطع ({mag})"


def verify_link(kind: str, url: str, referer: str) -> str:
    if kind == "MP4":
        return verify_file(url, referer)
    return verify_hls(url, referer)


_LITERAL_RE = re.compile(r"""(['"])(.*?)\1""")


def eval_js_concat(expr: str) -> str:
    out = ""
    for m in _LITERAL_RE.finditer(expr):
        lit = m.group(2)
        pos = m.end()
        while True:
            sm = re.match(r"\s*\)?\s*\.substring\((\d+)\)", expr[pos:])
            if not sm:
                break
            lit = lit[int(sm.group(1)):]
            pos += sm.end()
        out += lit
    return out


def extract_streamtape(embed_url: str) -> list[dict]:
    """StreamTape (MP4): يعيد بناء روابط get_video من JS ثم يتبع 302 يحلّها للملف."""
    REDIRECT_CODES = (301, 302, 303, 307, 308)
    try:
        r = requests.get(embed_url, headers=UA, timeout=30)
        html = r.text
    except requests.RequestException:
        return []
    ref = embed_url.split("/e/")[0]

    links = []
    for m in re.finditer(r"innerHTML\s*=\s*([^;]+);", html):
        built = eval_js_concat(m.group(1))
        if "get_video" in built:
            links.append(built.lstrip("/"))
    looks_legit = [u for u in links if "streamtape.cc" in u]
    links = [u if u.startswith("http") else "https://" + u for u in dict.fromkeys(looks_legit or links)]

    out = []
    session = requests.Session()
    for get_url in links:
        try:
            rr = session.get(get_url + "&stream=1", headers={"Referer": ref}, allow_redirects=False, timeout=(5, 15))
            if rr.status_code in REDIRECT_CODES and rr.headers.get("Location"):
                loc = rr.headers["Location"]
                label = "??p"
                mq = re.search(r"(\d{3,4})p", loc, re.I)
                if mq:
                    label = mq.group(1) + "p"
                out.append({"kind": "MP4", "label": label, "res": "1920x1080" if label == "1080p" else "", "url": loc})
        except requests.RequestException:
            pass
    return out


def extract_vidmoly(embed_url: str) -> list[dict]:
    """VidMoly (HLS): المصدر موجود نصياً في الصفحة ثم نحلل قائمة الجودات."""
    try:
        r = requests.get(embed_url, headers=UA, timeout=40)
        html = r.text
    except requests.RequestException:
        return []
    m = re.search(r"sources:\s*\[\s*\{\s*file:\s*['\"]([^'\"]+\.m3u8[^'\"]*)['\"]", html, re.I)
    if not m:
        return []
    master = m.group(1)
    base = embed_url.split("/embed-")[0]
    txt = fetch_master(master, base + "/")
    if not txt:
        return [{"kind": "HLS", "res": "", "url": master}]
    variants = parse_variants(txt, master)
    return [{"kind": "HLS", "label": v["label"], "res": v["res"], "url": v["url"]} for v in variants]


def display_results(name: str, results: list[dict], embed: str = "", verify: bool = False):
    if not results:
        print(f"   ✗ لا يوجد رابط مستخرج (بنية مختلفة أو حماية)")
        return 0
    ok = 0
    if verify:
        print(f"   ✓ {len(results)} نتيجة")
        print(f"   {'#':<4}{'QUALITY':<12}{'VERDICT':<34}URL")
        for i, r in enumerate(results, 1):
            verdict = verify_link(r["kind"], r["url"], embed)
            if verdict.startswith("✓"):
                ok += 1
            print(f"   {i:<4}{r.get('label','?'):<12}{verdict:<34}{r['url']}")
    else:
        print(f"   ✓ {len(results)} نتيجة")
        print(f"   {'#':<4}{'QUALITY':<12}{'TYPE':<7}URL")
        for i, r in enumerate(results, 1):
            st = "MP4" if r["kind"] == "MP4" else probe(r["url"], "")
            if r["kind"] == "MP4" or st == "200":
                ok += 1
            print(f"   {i:<4}{r.get('label','?'):<12}{r['kind']:<7}{r['url']}")
    return ok


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    verify = "--verify" in sys.argv or "--check" in sys.argv
    print("=" * 92)
    print("  VideoTube ALL - كل السيرفرات + جميع الجودات" + ("  [وضع تحقق: --verify]" if verify else ""))
    print("=" * 92)

    total_ok = 0
    for name, embed in SERVERS:
        print()
        print(f"▌ {name}  ({embed})")
        print("-" * 92)

        if "streamtape" in name.lower():
            total_ok += display_results(name, extract_streamtape(embed), embed, verify)
            continue
        if "vidmoly" in name.lower():
            total_ok += display_results(name, extract_vidmoly(embed), embed, verify)
            continue

        try:
            master = unpack_master_url(embed)
        except Exception as e:
            master = None
            print(f"   ✗ unpack failed: {str(e)[:80]}")
        if not master:
            print("   ✗ لا يوجد رابط master مستخرج (بنية مختلفة أو حماية)")
            continue

        variants = []
        for ref in (embed, ""):
            txt = fetch_master(master, ref)
            if txt:
                variants = parse_variants(txt, master)
                break
        if not variants:
            print(f"   ✗ master موجود لكن لا يمكن جلب الجودات: {master[:90]}")
            continue

        print(f"   ✓ master.m3u8  ({len(variants)} جودة)")
        if verify:
            print(f"   {'#':<4}{'QUALITY':<12}{'VERDICT':<34}URL")
        else:
            print(f"   {'#':<4}{'QUALITY':<12}{'STATUS':<8}URL")
        for i, v in enumerate(variants, 1):
            st = probe(v["url"], embed)
            if st == "200":
                total_ok += 1
            if verify:
                verdict = verify_link("HLS", v["url"], embed)
                if verdict.startswith("✓"):
                    total_ok += 1
                print(f"   {i:<4}{v['label']:<12}{verdict:<34}{v['url']}")
            else:
                print(f"   {i:<4}{v['label']:<12}{st:<8}{v['url']}")

    print()
    print("=" * 92)
    print(f"المجموع: {total_ok} رابط قابل للتشغيل من كل السيرفرات")
    print("-" * 92)
    print("الدليل:")
    print("  MP4  = رابط ملف مباشر نهائي (تفتحه/تحمّله كاملاً)")
    print("  HLS  = قائمة جودة adaptative (يقرؤها VLC/mpv/ومشغلو HLS)")
    print("  --verify = يفحص كل رابط: فيديو؟ سليم؟ ميت؟ تالف؟")
    print("=" * 92)


if __name__ == "__main__":
    main()