# ==============================================================================
# File: backend/app.py
# Description: FastAPI microservice for universal web video extraction and streaming.
# Module: Backend Microservice
# Purpose: Inspects web pages, unpacks obfuscated scripts (Dean Edwards p,a,c,k,e,d),
#          parses multi-quality HLS streams, proxies video chunks with Referer headers,
#          and serves an embedded Hls.js web player for seamless in-app streaming.
# Consumers: Consumed by Flutter Web & Mobile client via /extract, /proxy, and /player.
# Notes: Fully permissive CORS enabled to support cross-origin requests from any browser.
# ==============================================================================

import asyncio
import os
import re
import time
import urllib.parse
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
import httpx

app = FastAPI(
    title="Universal Video Extractor API",
    version="1.3.0",
    description="Extracts and streams video streams across all platforms.",
)

# Enable CORS for Flutter Web and cross-origin requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ExtractRequest(BaseModel):
    url: str

class VideoQuality(BaseModel):
    label: str
    url: str
    resolution: Optional[str] = None

class ExtractResponse(BaseModel):
    success: bool
    title: str
    thumbnail: Optional[str] = None
    duration: Optional[float] = None
    stream_url: str
    qualities: List[VideoQuality] = []
    headers: Dict[str, str] = {}
    error: Optional[str] = None

def unpack_dean_edwards(packed: str) -> Optional[str]:
    """Unpack Dean Edwards p,a,c,k,e,d JavaScript obfuscation."""
    match = re.search(r"}\s*\('(.*)',\s*(\d+),\s*(\d+),\s*'([^']*)'\.split\('\|'\)", packed)
    if not match:
        match = re.search(r"}\s*\('(.*)',\s*(\w+),\s*(\d+),\s*'([^']*)'\.split\('\|'\)", packed)
    if not match:
        return None

    payload, radix_str, count_str, symtab = match.groups()
    try:
        radix = int(radix_str)
        count = int(count_str)
    except ValueError:
        return None

    keywords = symtab.split("|")
    chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def base_n(num: int, b: int) -> str:
        if num == 0:
            return chars[0]
        res = []
        while num > 0:
            res.append(chars[num % b])
            num //= b
        return "".join(reversed(res))

    lookup = {}
    for i in range(count):
        token = base_n(i, radix) if radix > 10 else str(i)
        lookup[token] = keywords[i] if i < len(keywords) and keywords[i] else token

    def replace_token(m):
        word = m.group(0)
        return lookup.get(word, word)

    return re.sub(r"\b\w+\b", replace_token, payload)

async def parse_m3u8_qualities(
    master_url: str,
    referer: str,
    user_agent: str,
) -> List[VideoQuality]:
    """Fetches master.m3u8 playlist and extracts individual resolution streams."""
    qualities: List[VideoQuality] = []
    try:
        ref_split = urllib.parse.urlsplit(referer) if referer else None
        origin = f"{ref_split.scheme}://{ref_split.netloc}" if (ref_split and ref_split.netloc) else "https://google.com"
        headers = {
            "User-Agent": user_agent,
            "Referer": referer,
            "Origin": origin,
            "Accept": "*/*",
        }
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=8.0) as client:
            resp = await client.get(master_url)
            if resp.status_code == 200:
                lines = resp.text.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(lines):
                        uri_line = lines[i + 1].strip()
                        if uri_line and not uri_line.startswith("#"):
                            full_url = urllib.parse.urljoin(master_url, uri_line)
                            if "?" not in uri_line and "?" in master_url:
                                master_query = urllib.parse.urlparse(master_url).query
                                if master_query:
                                    full_url += ("&" if "?" in full_url else "?") + master_query
                            res_match = re.search(r"RESOLUTION=(\d+x\d+)", line)
                            label = "Auto"
                            if res_match:
                                res_str = res_match.group(1)
                                height = res_str.split("x")[1]
                                label = f"{height}p"
                            qualities.append(
                                VideoQuality(
                                    label=label,
                                    url=full_url,
                                    resolution=res_match.group(1) if res_match else None,
                                )
                            )
    except Exception:
        pass

    def get_height(q: VideoQuality) -> int:
        match = re.search(r"(\d+)p", q.label)
        return int(match.group(1)) if match else 0

    qualities.sort(key=get_height, reverse=True)
    qualities.insert(0, VideoQuality(label="Auto (تلقائي)", url=master_url))
    return qualities

@app.get("/")
def health_check():
    return {
        "status": "online",
        "service": "Universal Video Extractor API",
        "endpoints": ["/extract", "/proxy", "/player", "/download"],
    }

@app.get("/proxy.m3u8")
@app.get("/proxy.ts")
@app.get("/proxy")
async def proxy_stream(url: str, referer: Optional[str] = None):
    """Proxies m3u8 playlists and video chunks with correct Referer and Origin headers."""
    clean_url = urllib.parse.unquote(url)
    clean_referer = urllib.parse.unquote(referer) if referer else "https://google.com/"
    clean_referer = re.sub(r"^(https?://)www\.", r"\1", clean_referer)

    # Auto-resolve host-specific referers to prevent 403 Forbidden on strict CDNs
    if "mp4upload" in clean_url:
        clean_referer = "https://www.mp4upload.com/"
        origin = "https://www.mp4upload.com"
    elif "uqload" in clean_url:
        clean_referer = "https://uqload.vc/"
        origin = "https://uqload.vc"
    elif "mxcontent" in clean_url or "mixdrop" in clean_url:
        clean_referer = "https://mixdrop.ag/"
        origin = "https://mixdrop.ag"
    elif "vidara" in clean_url or "97bf1" in clean_url:
        clean_referer = "https://vidaraa.cc/"
        origin = "https://vidaraa.cc"
    else:
        ref_split = urllib.parse.urlsplit(clean_referer)
        origin = f"{ref_split.scheme}://{ref_split.netloc}" if ref_split.netloc else "https://google.com"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": clean_referer,
        "Origin": origin,
        "Accept": "*/*",
    }

    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20.0) as client:
            resp = await client.get(clean_url)
            content_type = resp.headers.get("content-type", "application/octet-stream")

            if "mpegurl" in content_type or clean_url.endswith(".m3u8") or ".m3u8" in clean_url:
                text = resp.text
                lines = text.splitlines()
                rewritten = []
                for line in lines:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        abs_url = urllib.parse.urljoin(clean_url, stripped)
                        if "?" not in stripped and "?" in clean_url:
                            q = urllib.parse.urlparse(clean_url).query
                            if q:
                                abs_url = abs_url + ("&" if "?" in abs_url else "?") + q
                        encoded_target = urllib.parse.quote(abs_url)
                        encoded_ref = urllib.parse.quote(clean_referer)
                        if ".m3u8" in abs_url:
                            rewritten.append(f"/proxy.m3u8?url={encoded_target}&referer={encoded_ref}")
                        else:
                            rewritten.append(f"/proxy.ts?url={encoded_target}&referer={encoded_ref}")
                    else:
                        rewritten.append(line)
                return Response(
                    content="\n".join(rewritten),
                    media_type="application/vnd.apple.mpegurl",
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Content-Type": "application/vnd.apple.mpegurl",
                    },
                )

            # Enforce video/mp2t for TS segments to satisfy mobile ExoPlayer container sniffing
            if ".ts" in clean_url or "mp2t" in content_type:
                content_type = "video/mp2t"

            return Response(
                content=resp.content,
                media_type=content_type,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Content-Type": content_type,
                },
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Proxy error: {str(e)}")


@app.get("/player", response_class=HTMLResponse)
def serve_player(src: str, referer: Optional[str] = None, title: Optional[str] = None):
    """Serves a clean, responsive HTML5 player with robust Hls.js support."""
    clean_src = urllib.parse.unquote(src)
    clean_ref = urllib.parse.unquote(referer) if referer else ""
    display_title = urllib.parse.unquote(title) if title else "مشغل الفيديو"

    if clean_ref:
        stream_src = f"/proxy.m3u8?url={urllib.parse.quote(clean_src)}&referer={urllib.parse.quote(clean_ref)}"
    else:
        stream_src = clean_src

    html_content = f"""<!DOCTYPE html>
<html lang="ar">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{display_title}</title>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        html, body {{
            width: 100%;
            height: 100%;
            background-color: #000;
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        video {{
            width: 100%;
            height: 100%;
            object-fit: contain;
            background-color: #000;
        }}
    </style>
</head>
<body>
    <video id="video" controls autoplay playsinline></video>
    <script>
        const video = document.getElementById('video');
        const videoSrc = '{stream_src}';

        function playDirect(src) {{
            video.src = src;
            const promise = video.play();
            if (promise !== undefined) {{
                promise.catch(function() {{
                    video.muted = true;
                    video.play();
                }});
            }}
        }}

        const isHls = videoSrc.includes('.m3u8') || videoSrc.includes('/hls/') || videoSrc.includes('.urlset');

        if (isHls && Hls.isSupported()) {{
            const hls = new Hls({{
                enableWorker: true,
                maxBufferLength: 30,
                maxMaxBufferLength: 60,
                startFragPrefetch: true,
            }});
            hls.loadSource(videoSrc);
            hls.attachMedia(video);
            hls.on(Hls.Events.MANIFEST_PARSED, function() {{
                const playPromise = video.play();
                if (playPromise !== undefined) {{
                    playPromise.catch(function() {{
                        video.muted = true;
                        video.play();
                    }});
                }}
            }});
            hls.on(Hls.Events.ERROR, function(event, data) {{
                if (data.fatal) {{
                    console.warn('Hls fatal error, falling back to direct video element:', data.details);
                    hls.destroy();
                    playDirect(videoSrc);
                }}
            }});
        }} else if (isHls && video.canPlayType('application/vnd.apple.mpegurl')) {{
            playDirect(videoSrc);
        }} else {{
            // Universal Direct Native Playback for MP4, WebM, and Raw Streams
            playDirect(videoSrc);
        }}
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)

async def resolve_direct_video_stream(url: str, default_title: str = ""):
    """
    Direct Stream Extractor Engine.
    Resolves a single video host URL into playable media streams (.m3u8 or .mp4).
    Uses specialized fast-path resolvers (Vidara, Mp4Upload), Dean Edwards unpacking,
    HTML5 video sources, and yt-dlp fallback.
    """
    url_clean = re.sub(r"^(https?://)www\.", r"\1", url.strip())
    parsed = urllib.parse.urlsplit(url_clean)
    domain = parsed.netloc
    origin = f"{parsed.scheme}://{domain}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": url_clean,
        "Origin": origin,
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }

    # Fast-Path 1: Vidara Provider
    if "vidara" in domain:
        try:
            filecode_match = re.search(r"/e/([a-zA-Z0-9_-]+)", url_clean)
            if filecode_match:
                filecode = filecode_match.group(1)
                api_url = f"{parsed.scheme}://{domain}/api/stream"
                async with httpx.AsyncClient(headers=headers, timeout=8.0) as client:
                    api_resp = await client.post(
                        api_url,
                        json={"filecode": filecode, "device": "web"},
                        headers={"Content-Type": "application/json", "Referer": url_clean},
                    )
                    if api_resp.status_code == 200:
                        data = api_resp.json()
                        stream_url = data.get("streaming_url")
                        if stream_url:
                            thumbnail = data.get("thumbnail")
                            title = data.get("title") or default_title or domain
                            parsed_qualities = []
                            if ".m3u8" in stream_url:
                                parsed_qualities = await parse_m3u8_qualities(stream_url, url_clean, headers["User-Agent"])
                            return {
                                "stream_url": stream_url,
                                "qualities": parsed_qualities if parsed_qualities else [VideoQuality(label="Original", url=stream_url)],
                                "title": title,
                                "thumbnail": thumbnail,
                                "duration": None,
                            }
        except Exception:
            pass

    # Fast-Path 2: Mp4Upload Provider
    if "mp4upload" in domain:
        try:
            async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=8.0) as client:
                resp = await client.get(url_clean)
                html = resp.text
                mp4_match = re.search(r'https?://[^"\'<>\s]+/video\.mp4', html)
                poster_match = re.search(r'player\.poster\(["\'](https?://[^"\']+)["\']\)', html)
                if mp4_match:
                    video_url = mp4_match.group(0)
                    return {
                        "stream_url": video_url,
                        "qualities": [VideoQuality(label="MP4", url=video_url)],
                        "title": default_title or domain,
                        "thumbnail": poster_match.group(1) if poster_match else None,
                        "duration": None,
                    }
        except Exception:
            pass

    # Fast-Path 3: Mixdrop Provider
    if "mixdrop" in domain:
        try:
            async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=8.0) as client:
                resp = await client.get(url_clean)
                html = resp.text
                scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL | re.I)
                for script in scripts:
                    if "eval(function(p,a,c,k,e,d)" in script:
                        unpacked = unpack_dean_edwards(script)
                        if unpacked:
                            mix_match = re.search(r'MDCore\.wurl\s*=\s*["\']([^"\']+)["\']', unpacked)
                            if mix_match:
                                video_url = mix_match.group(1)
                                if video_url.startswith("//"):
                                    video_url = f"https:{video_url}"
                                poster_match = re.search(r'MDCore\.poster\s*=\s*["\']([^"\']+)["\']', unpacked)
                                poster_url = poster_match.group(1) if poster_match else None
                                if poster_url and poster_url.startswith("//"):
                                    poster_url = f"https:{poster_url}"
                                return {
                                    "stream_url": video_url,
                                    "qualities": [VideoQuality(label="Original", url=video_url)],
                                    "title": default_title or domain,
                                    "thumbnail": poster_url,
                                    "duration": None,
                                }
        except Exception:
            pass

    # Standard Scraping: Dean Edwards & HTML5
    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=10.0) as client:
            resp = await client.get(url_clean)
            html = resp.text

        title_match = re.search(r"<title>(.*?)</title>", html, re.I)
        page_title = title_match.group(1).strip() if title_match else (default_title or domain)

        # Dean Edwards packed scripts
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL | re.I)
        for script in scripts:
            if "eval(function(p,a,c,k,e,d)" in script:
                unpacked = unpack_dean_edwards(script)
                if unpacked:
                    media_links = re.findall(
                        r'https?://[^\s"\'<>]+\.(?:m3u8|mp4|webm)[^\s"\'<>]*',
                        unpacked,
                        re.I,
                    )
                    file_match = re.search(r'file\s*:\s*["\'](https?://[^"\']+)["\']', unpacked, re.I)
                    if file_match and file_match.group(1) not in media_links:
                        media_links.insert(0, file_match.group(1))

                    img_match = re.search(r'image\s*:\s*["\'](https?://[^"\']+)["\']', unpacked, re.I)
                    thumbnail = img_match.group(1) if img_match else None

                    dur_match = re.search(r'duration\s*:\s*["\']?([\d\.]+)["\']?', unpacked, re.I)
                    duration = float(dur_match.group(1)) if dur_match else None

                    if media_links:
                        main_stream = media_links[0]
                        qualities: List[VideoQuality] = []
                        if ".m3u8" in main_stream or "/hls/" in main_stream or ".urlset" in main_stream:
                            qualities = await parse_m3u8_qualities(
                                main_stream,
                                url_clean,
                                headers["User-Agent"],
                            )
                        if not qualities:
                            qualities = [VideoQuality(label="Original", url=main_stream)]
                        return {
                            "stream_url": main_stream,
                            "qualities": qualities,
                            "title": page_title,
                            "thumbnail": thumbnail,
                            "duration": duration,
                        }

        # Direct HTML5 <source> or <video> tags
        direct_sources = re.findall(
            r'<source[^>]+src=["\']([^"\']+)["\']',
            html,
            re.I,
        ) or re.findall(
            r'<video[^>]+src=["\']([^"\']+)["\']',
            html,
            re.I,
        )
        if direct_sources:
            src = urllib.parse.urljoin(url_clean, direct_sources[0])
            return {
                "stream_url": src,
                "qualities": [VideoQuality(label="Original (الأصلية)", url=src)],
                "title": page_title,
                "thumbnail": None,
                "duration": None,
            }
    except Exception:
        pass

    # Generic Fallback: yt-dlp (run in thread to prevent blocking event loop)
    def _run_ytdlp():
        try:
            import yt_dlp
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": False,
                "socket_timeout": 4,
                "http_headers": headers,
                "nocheckcertificate": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url_clean, download=False)
                if info:
                    qualities = []
                    formats = info.get("formats", [])
                    for f in formats:
                        f_url = f.get("url")
                        if f_url and f.get("vcodec") != "none":
                            res = f.get("format_note") or f.get("resolution") or "Video"
                            qualities.append(VideoQuality(label=res, url=f_url, resolution=res))
                    main_url = info.get("url") or (qualities[0].url if qualities else None)
                    if main_url:
                        return {
                            "stream_url": main_url,
                            "qualities": qualities if qualities else [VideoQuality(label="Default", url=main_url)],
                            "title": info.get("title") or default_title or domain,
                            "thumbnail": info.get("thumbnail"),
                            "duration": info.get("duration"),
                        }
        except Exception:
            pass
        return None

    try:
        ytdlp_res = await asyncio.wait_for(asyncio.to_thread(_run_ytdlp), timeout=5.0)
        if ytdlp_res:
            return ytdlp_res
    except Exception:
        pass

    return None

@app.post("/extract", response_model=ExtractResponse)
async def extract_video(req: ExtractRequest):
    url = req.url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid URL protocol")

    # Normalize input URL: strip leading 'www.' from host while strictly preserving the full embed path
    clean_url = re.sub(r"^(https?://)www\.", r"\1", url)
    url_split = urllib.parse.urlsplit(clean_url)
    domain = url_split.netloc
    referer = clean_url
    origin = f"{url_split.scheme}://{domain}"

    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": referer,
        "Origin": origin,
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }
    stream_headers = {
        "Referer": referer,
        "Origin": origin,
        "User-Agent": request_headers["User-Agent"],
    }

    try:
        # Layer 1: Hub / Container Detection (e.g. MegaMax multi-server hub)
        is_megamax_hub = "megamax.me" in url_split.netloc
        if not is_megamax_hub:
            async with httpx.AsyncClient(headers=request_headers, follow_redirects=True, timeout=12.0) as check_client:
                resp = await check_client.get(clean_url)
                html = resp.text
                if "files/mirror/video" in html or "files\\/mirror\\/video" in html:
                    is_megamax_hub = True

        if is_megamax_hub:
            try:
                # Fetch Hub page and Inertia streams
                async with httpx.AsyncClient(headers=request_headers, follow_redirects=True, timeout=12.0) as hub_client:
                    page_resp = await hub_client.get(clean_url)
                    hub_html = page_resp.text

                name_match = re.search(r'"name":"([^"]+)"', hub_html)
                hub_title = name_match.group(1).replace(r'\"', '"') if name_match else domain

                version_match = re.search(r'"version":"([^"]+)"', hub_html)
                version = version_match.group(1) if version_match else "a601a2d0d16b8ae7121ceb1fd46c1f5a"

                inertia_headers = dict(request_headers)
                inertia_headers.update({
                    "X-Inertia": "true",
                    "X-Inertia-Version": version,
                    "X-Inertia-Partial-Component": "files/mirror/video",
                    "X-Inertia-Partial-Data": "streams",
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                })

                async with httpx.AsyncClient(headers=inertia_headers, follow_redirects=True, timeout=12.0) as data_client:
                    data_resp = await data_client.get(clean_url)
                    if data_resp.status_code == 200:
                        hub_data = data_resp.json()
                        streams = hub_data.get("props", {}).get("streams", {}).get("data", [])
                        
                        # Gather all mirrors to resolve them into real playable video streams
                        mirror_tasks = []
                        mirror_meta = []
                        for group in streams:
                            res_label = group.get("label", "Default").replace(" (source)", "").strip()
                            resolution = group.get("resolution")
                            for mirror in group.get("mirrors", []):
                                driver = mirror.get("driver", "Server")
                                link = mirror.get("link", "")
                                if link.startswith("//"):
                                    link = f"https:{link}"
                                if link:
                                    mirror_meta.append({
                                        "label": f"{res_label} - {driver}",
                                        "resolution": resolution,
                                        "driver": driver,
                                        "link": link
                                    })
                                    mirror_tasks.append(resolve_direct_video_stream(link, default_title=hub_title))

                        # Execute video stream extraction for all mirrors in parallel
                        results = await asyncio.gather(*mirror_tasks, return_exceptions=True)
                        verified_qualities: List[VideoQuality] = []
                        primary_stream_url = None
                        primary_thumbnail = None
                        primary_duration = None

                        for meta, res in zip(mirror_meta, results):
                            if isinstance(res, dict) and res.get("stream_url"):
                                s_url = res["stream_url"]
                                if not primary_stream_url:
                                    primary_stream_url = s_url
                                    primary_thumbnail = res.get("thumbnail")
                                    primary_duration = res.get("duration")

                                verified_qualities.append(
                                    VideoQuality(
                                        label=meta["label"],
                                        url=s_url,
                                        resolution=meta["resolution"],
                                    )
                                )

                        if verified_qualities and primary_stream_url:
                            return ExtractResponse(
                                success=True,
                                title=hub_title,
                                thumbnail=primary_thumbnail,
                                duration=primary_duration,
                                stream_url=primary_stream_url,
                                qualities=verified_qualities,
                                headers=stream_headers,
                            )
            except Exception:
                pass

        # Layer 2: Standard Single-Video Extraction
        resolved = await resolve_direct_video_stream(clean_url, default_title=domain)
        if resolved and resolved.get("stream_url"):
            return ExtractResponse(
                success=True,
                title=resolved.get("title") or domain,
                thumbnail=resolved.get("thumbnail"),
                duration=resolved.get("duration"),
                stream_url=resolved["stream_url"],
                qualities=resolved.get("qualities") or [VideoQuality(label="Original", url=resolved["stream_url"])],
                headers=stream_headers,
            )

        raise HTTPException(
            status_code=404,
            detail="No playable video stream or source could be extracted from this page.",
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Extraction failure: {str(e)}")

