"""
File: server.py
Role: Main HTTP API server for video extraction and proxying.
Purpose: Deployed on Render to handle video hosts that block Cloudflare Datacenter IPs.
         Integrates yt-dlp universal extraction and direct Packer unpacking.
Consumers: Cloudflare Worker (/info, /hls, /proxy) and web player frontend.
"""

import os
import re
import urllib.parse
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import requests
import yt_dlp

app = FastAPI(title="Video Extractor API", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

def get_media_referer(u: str, explicit_ref: Optional[str] = None) -> str:
    if explicit_ref:
        return explicit_ref
    try:
        host = urllib.parse.urlparse(u).hostname.lower()
        if "cdn-video.xyz" in host:
            return "https://vidtube.cam/"
        if "1vid." in host:
            return "https://1vid.xyz/"
        if "vmnow." in host or "vidmoly." in host:
            return "https://vidmoly.org/"
        if "cdnz." in host or "vidspeed." in host:
            return "https://vidspeed.org/"
        if "uqload." in host:
            return "https://uqload.vc/"
        if "mixdrop." in host or "soakysecrets." in host:
            return "https://mixdrop.top/"
        if "streamtape." in host or "tapecontent." in host:
            return "https://streamtape.cc/"
        if "premilkyway." in host or "hlswish." in host:
            return "https://hlswish.com/"
        if "acek-cdn." in host or "dramiyos-cdn." in host or "earnvids." in host:
            return "https://morencius.com/"
        return f"{urllib.parse.urlparse(u).scheme}://{urllib.parse.urlparse(u).netloc}/"
    except Exception:
        return ""

def unpack_packer(html: str) -> str:
    out = []
    matches = re.finditer(r"eval\(function\(p,a,c,k,e,d\)", html)
    for m in matches:
        start = m.start()
        open_paren = html.find("(", start + 4)
        if open_paren == -1:
            continue
        depth = 0
        end = -1
        for i in range(open_paren, len(html)):
            ch = html[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            continue
        args_str = html[open_paren + 1:end].strip()
        parts = [p.strip() for p in args_str.split(",")]
        if len(parts) >= 4:
            out.append(args_str)
    return "\n".join(out)

def extract_with_ytdlp(url: str) -> Optional[Dict[str, Any]]:
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'best',
        'extract_flat': False,
        'user_agent': UA,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None
            
            title = info.get('title', '')
            formats = info.get('formats', [])
            hls_urls = set()
            mp4_urls = set()
            variants = []

            for f in formats:
                f_url = f.get('url', '')
                if not f_url or not f_url.startswith('http'):
                    continue
                ext = f.get('ext', '').lower()
                h = f.get('height') or 0
                w = f.get('width') or 0
                bw = f.get('tbr') or 0
                format_note = f.get('format_note', '')

                if '.m3u8' in f_url or ext == 'm3u8':
                    hls_urls.add(f_url)
                    variants.append({
                        "url": f_url,
                        "height": h,
                        "width": w,
                        "bandwidth": int(bw * 1000) if bw else 0,
                        "label": f"{h}p" if h else (format_note or "auto")
                    })
                elif '.mp4' in f_url or ext in ['mp4', 'webm', 'mkv']:
                    mp4_urls.add(f_url)

            return {
                "title": title,
                "hls": list(hls_urls),
                "mp4": list(mp4_urls),
                "variants": variants
            }
    except Exception:
        return None

def extract_direct_regex(url: str) -> Dict[str, Any]:
    headers = {
        "User-Agent": UA,
        "Referer": url,
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        html = resp.text
    except Exception as e:
        return {"url": url, "error": str(e), "hls": [], "mp4": [], "variants": []}

    title_match = re.search(r'<title>([^<]+)</title>', html, re.I)
    title = title_match.group(1).strip() if title_match else ""

    hls = set()
    mp4 = set()

    # Direct URLs
    direct_m3u8 = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html, re.I)
    for u in direct_m3u8:
        hls.add(u)

    # Streamtape robotlink
    tape_match = re.search(r"document\.getElementById\(['\"](?:robotlink|ideolink)['\"]\)\.innerHTML\s*=\s*['\"]([^'\"]+)['\"]\s*\+\s*\(['\"]([^'\"]+)['\"]\)\.substring\((\d+)\)(?:\.substring\((\d+)\))?", html, re.I)
    if tape_match:
        part1 = tape_match.group(1)
        part2 = tape_match.group(2)
        sub1 = int(tape_match.group(3) or "0")
        sub2 = int(tape_match.group(4) or "0")
        if sub1:
            part2 = part2[sub1:]
        if sub2:
            part2 = part2[sub2:]
        full = part1 + part2
        if full.startswith("//"):
            full = "https:" + full
        mp4.add(full)

    # Packed JS sources
    packed_sources = re.findall(r'(?:file|source|src)\s*[:=]\s*["\'](https?://[^"\']+)["\']', html, re.I)
    for u in packed_sources:
        if ".m3u8" in u:
            hls.add(u)
        elif ".mp4" in u:
            mp4.add(u)

    return {
        "url": url,
        "title": title,
        "hls": list(hls),
        "mp4": list(mp4),
        "variants": [{"url": u, "label": "auto"} for u in hls]
    }

@app.get("/")
@app.get("/health")
def health_check():
    return {"status": "ok", "service": "video-extractor-api", "version": "3.0"}

@app.get("/info")
def get_info(v: Optional[str] = None, url: Optional[str] = None):
    target = v or url
    if not target:
        return JSONResponse({"error": "missing parameter v or url"}, status_code=400)

    # 1. Try yt-dlp first
    data = extract_with_ytdlp(target)
    if data and (data["hls"] or data["mp4"]):
        data["url"] = target
        return data

    # 2. Fallback to direct regex/unpack
    direct_data = extract_direct_regex(target)
    return direct_data

@app.get("/hls")
def proxy_hls(u: str = Query(...), ref: Optional[str] = None):
    referer = get_media_referer(u, ref)
    headers = {"User-Agent": UA, "Referer": referer}
    try:
        r = requests.get(u, headers=headers, timeout=12)
        content = r.text
        
        # Rewrite relative URLs to absolute or proxied
        lines = content.split("\n")
        out = []
        for line in lines:
            trimmed = line.strip()
            if not trimmed or trimmed.startswith("#"):
                out.append(line)
                continue
            abs_url = urllib.parse.urljoin(u, trimmed)
            out.append(f"/proxy?u={urllib.parse.quote(abs_url)}&ref={urllib.parse.quote(referer)}")
        
        return Response(
            content="\n".join(out),
            media_type="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

@app.get("/proxy")
def proxy_media(request: Request, u: str = Query(...), ref: Optional[str] = None):
    referer = get_media_referer(u, ref)
    headers = {
        "User-Agent": UA,
        "Referer": referer,
        "Origin": referer if referer else f"{urllib.parse.urlparse(u).scheme}://{urllib.parse.urlparse(u).netloc}"
    }
    
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    try:
        r = requests.get(u, headers=headers, stream=True, timeout=15)
        
        resp_headers = {}
        for k in ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"]:
            if k in r.headers:
                resp_headers[k] = r.headers[k]
        resp_headers["Access-Control-Allow-Origin"] = "*"
        resp_headers["Cache-Control"] = "public, max-age=3600"

        return StreamingResponse(
            r.iter_content(chunk_size=64 * 1024),
            status_code=r.status_code,
            headers=resp_headers
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
