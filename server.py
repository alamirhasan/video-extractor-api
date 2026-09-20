import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
PORT = int(os.environ.get("PORT", "8000"))
HOSTS = {"vidtube.one", "down.vidtube.one"}


def fetch(url, referer=None):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read()


def find_matching(s, open_idx):
    depth = 0
    quote = None
    esc = False
    for idx in range(open_idx, len(s)):
        ch = s[idx]
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return idx
    return -1


def js_split(s):
    parts, cur, quote, esc, depth = [], [], None, False, 0
    for ch in s:
        if quote:
            cur.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            cur.append(ch)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts


def unescape(s):
    if s and s[0] in "'\"":
        s = s[1:-1]
    return s.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")


def unpack_packer(html):
    for m in re.finditer(r"eval\(function\(p,a,c,k,e,d\)", html):
        i = html.find("return p}", m.start())
        if i < 0 or i > m.start() + 400:
            continue
        open_idx = html.find("(", i)
        close_idx = find_matching(html, open_idx)
        if close_idx < 0:
            continue
        parts = js_split(html[open_idx + 1:close_idx])
        if len(parts) < 4:
            continue
        p = unescape(parts[0])
        radix = int(parts[1], 0)
        count = int(parts[2], 0)
        tokens = unescape(parts[3]).split("|")
        dec = {}
        for tx in range(count):
            n = tx
            digits = ""
            while True:
                digits = "0123456789abcdefghijklmnopqrstuvwxyz"[n % radix] + digits
                n //= radix
                if n == 0:
                    break
            dec[digits] = tokens[tx] if tx < len(tokens) else ""
        out = p
        for enc, word in sorted(dec.items(), key=lambda kv: -len(kv[0])):
            if word:
                out = re.sub(r"\b" + enc + r"\b", word, out)
        if "jwplayer" in out or "sources" in out:
            return out
    return None


def page_title(html, video_id):
    m = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    if m:
        return m.group(1).strip()
    m = re.search(r"<title>([^<]*)</title>", html, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return video_id


def parse_master(body, master_url=None):
    variants = []
    for m in re.finditer(r"#EXT-X-STREAM-INF:[^\n]*BANDWIDTH=(\d+)[^\n]*RESOLUTION=(\d+)x(\d+)[^\n]*\n(\S+)", body):
        variants.append({
            "bandwidth": int(m.group(1)),
            "width": int(m.group(2)),
            "height": int(m.group(3)),
            "label": quality_label(int(m.group(3))),
            "url": m.group(4),
        })
    return variants


def quality_label(h):
    if h >= 1080:
        return "1080p"
    if h >= 720:
        return "720p"
    if h >= 480:
        return "480p"
    return "240p"


def get_mp4_links(video_id):
    links = {}
    for s in ["x", "h", "n", "l"]:
        try:
            html = fetch(f"https://vidtube.one/d/{video_id}_{s}").decode("utf-8", "replace")
        except Exception:
            continue
        mp4s = re.findall(r"https://[^\"']+\.mp4\?t=[^\"'\s]+", html)
        if mp4s:
            links[s] = mp4s
    return links


def extract_id(value):
    value = value.strip()
    m = re.search(r"([a-z0-9]{12,})", value)
    return m.group(1) if m else None


def probe_audio(variant_base, suffix=""):
    found = {}
    for idx in range(1, 7):
        url = variant_base + "/index-v1-a%d.m3u8%s" % (idx, suffix)
        try:
            body = fetch(url).decode("utf-8", "replace")
        except Exception:
            continue
        segs = re.findall(r"(seg-\d+[^\s?]+)", body)
        if not segs:
            continue
        key = segs[0].split("?")[0]
        found.setdefault(key, []).append("a%d" % idx)
    return found


def extract(url):
    video_id = extract_id(url)
    if not video_id:
        return {"ok": False, "error": "could not find video id in: " + url}
    try:
        html = fetch("https://vidtube.one/%s.html" % video_id).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "vidtube page HTTP %s (blocked?)" % e.code}
    except Exception as e:
        return {"ok": False, "error": "vidtube page fetch failed: %s" % e}

    out = {"ok": True, "id": video_id, "title": page_title(html, video_id)}

    cfg = unpack_packer(html)
    master_url = None
    if cfg:
        m = re.search(r'file:"(https://[^"]+\.m3u8[^"]*)"', cfg)
        if m:
            master_url = m.group(1)

    if not master_url:
        m = re.search(r'https://[^"\'<>\s]+\.m3u8[^"\'<>\s]*', html)
        if m:
            master_url = m.group(0)

    if master_url:
        out["master"] = master_url
        try:
            body = fetch(master_url, referer="https://vidtube.one/%s.html" % video_id).decode("utf-8", "replace")
            v = parse_master(body)
            if v:
                out["variants"] = [{"url": u["url"], "width": u["width"], "height": u["height"],
                                    "bandwidth": u["bandwidth"], "label": u["label"]} for u in v]
            if v:
                try:
                    out["audio"] = probe_audio(v[0]["url"].split("?")[0],
                                               ("?" + v[0]["url"].split("?")[1]) if "?" in v[0]["url"] else "")
                except Exception:
                    pass
        except Exception as e:
            out["master_error"] = str(e)

    out["mp4"] = get_mp4_links(video_id)
    return out


class H(BaseHTTPRequestHandler):
    def _send(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            self._send(200, {"ok": True})
            return
        if u.path == "/extract":
            url = (q.get("url") or q.get("v") or [None])[0]
            if not url:
                self._send(400, {"ok": False, "error": "missing url"})
                return
            self._send(200, extract(url))
            return
        self._send(404, {"ok": False, "error": "not found"})


server = ThreadingHTTPServer(("0.0.0.0", PORT), H)
server.serve_forever()