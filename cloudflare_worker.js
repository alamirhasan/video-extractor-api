/**
 * cloudflare_worker.js - وسيط البث التدفقي السحابي فائق السرعة
 * ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 * الوظيفة والمسؤولية:
 *   - يعمل على حافة شبكة Cloudflare (Edge) في أكثر من 300 مركز بيانات حول العالم.
 *   - استقبال طلبات بث الفيديو ومقاطع HLS (.ts) وملفات MP4.
 *   - حقن ترويسة Referer المطلوبة لتخطي حظر السيرفرات (مثل Hlswish و VidMoly).
 *   - إعادة كتابة قوائم m3u8 ديناميكياً لتمرير كافة المقاطع ومفاتيح التشفير عبر Cloudflare.
 *   - تفعيل التخزين المؤقت الذكي (Edge Caching) لمقاطع الفيديو لتقليل زمن الاستجابة.
 *   - استهلاك باندويث غير محدود مجاناً (Unlimited Bandwidth) لتوفير موارد سيرفر بايثون.
 *
 * طريقة النشر في Cloudflare:
 *   1. ادخل إلى https://dash.cloudflare.com/
 *   2. توجه إلى Workers & Pages -> Create Application -> Create Worker.
 *   3. الصق هذا الكود بالكامل واضغط Deploy.
 */

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
  "Access-Control-Allow-Headers": "Range, Content-Type, Authorization, Accept, X-Requested-With",
  "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
  "Access-Control-Max-Age": "86400",
};

const DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";

function safeB64Decode(str) {
  if (!str) return "";
  try {
    // إصلاح علامات الحشو وتبديل الرموز الخاصة بـ URL-safe base64
    let clean = str.replace(/-/g, "+").replace(/_/g, "/");
    clean += "=".repeat((4 - (clean.length % 4)) % 4);
    return decodeURIComponent(escape(atob(clean)));
  } catch (e) {
    try {
      return atob(str);
    } catch (_) {
      return "";
    }
  }
}

function safeB64Encode(str) {
  try {
    return btoa(unescape(encodeURIComponent(str || ""))).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
  } catch (e) {
    return "";
  }
}

function rewritePlaylist(text, playlistUrl, referer, workerOrigin) {
  const lines = text.split("\n");
  const out = [];
  const uriRegex = /URI="([^"]+)"/g;

  for (let line of lines) {
    const s = line.trim();
    if (!s) {
      out.push(line);
      continue;
    }

    // مسارات المقاطع أو القوائم الفرعية
    if (!s.startsWith("#")) {
      const resolved = s.startsWith("http") ? s : new URL(s, playlistUrl).toString();
      const proxyUrl = `${workerOrigin}/proxy?u=${safeB64Encode(resolved)}&r=${safeB64Encode(referer)}`;
      out.push(proxyUrl);
    }
    // وسوم مفاتيح التشفير والخرائط
    else if (s.startsWith("#EXT-X-KEY:") || s.startsWith("#EXT-X-MAP:")) {
      const rewritten = line.replace(uriRegex, (match, rawUri) => {
        const fullUri = rawUri.startsWith("http") ? rawUri : new URL(rawUri, playlistUrl).toString();
        return `URI="${workerOrigin}/proxy?u=${safeB64Encode(fullUri)}&r=${safeB64Encode(referer)}"`;
      });
      out.push(rewritten);
    } else {
      out.push(line);
    }
  }

  return out.join("\n");
}

export default {
  async fetch(request, env, ctx) {
    // 1. التعامل مع طلبات التحقق المسبق CORS (OPTIONS)
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const url = new URL(request.url);

    // الصفحة الإرشادية عند فتح رابط Worker في المتصفح مباشرة
    if (url.pathname === "/") {
      return new Response(
        JSON.stringify({
          service: "Cloudflare Edge Video Streaming Proxy",
          status: "active",
          usage: "/proxy?u=<BASE64_URL>&r=<BASE64_REFERER>",
        }, null, 2),
        {
          headers: {
            "Content-Type": "application/json; charset=utf-8",
            ...CORS_HEADERS,
          },
        }
      );
    }

    // 2. نقطة نهاية البروكسي التدفقي (/proxy)
    if (url.pathname === "/proxy") {
      const params = url.searchParams;
      const targetParam = params.get("u") || params.get("url") || "";
      const refParam = params.get("r") || params.get("ref") || "";

      let targetUrl = safeB64Decode(targetParam);
      if (!targetUrl.startsWith("http")) {
        // تجربة القراءة كـ URL مباشر إذا لم يكن base64
        targetUrl = targetParam;
      }

      let referer = safeB64Decode(refParam);
      if (!referer.startsWith("http") && refParam.startsWith("http")) {
        referer = refParam;
      }

      if (!targetUrl || (!targetUrl.startsWith("http://") && !targetUrl.startsWith("https://"))) {
        return new Response("Invalid or missing target URL", { status: 400, headers: CORS_HEADERS });
      }

      // تجهيز ترويسات الطلب الموجه إلى سيرفر الفيديو الأصلي
      const forwardHeaders = new Headers();
      forwardHeaders.set("User-Agent", DEFAULT_UA);
      if (referer) {
        forwardHeaders.set("Referer", referer);
      }

      // تمرير ترويسة Range إذا كان المشغل يطلب جزءاً محدداً
      const clientRange = request.headers.get("Range");
      if (clientRange) {
        forwardHeaders.set("Range", clientRange);
      }

      try {
        // إعداد التخزين المؤقت الذكي على حافة Cloudflare
        const isSegment = targetUrl.includes(".ts") || targetUrl.includes(".m4s");
        const fetchOptions = {
          method: "GET",
          headers: forwardHeaders,
          redirect: "follow",
        };

        if (isSegment) {
          fetchOptions.cf = {
            cacheEverything: true,
            cacheTtl: 3600, // تخزين المقطع لمدة ساعة في أقرب سيرفر للمستخدم
          };
        }

        const upstreamResponse = await fetch(targetUrl, fetchOptions);

        if (!upstreamResponse.ok && upstreamResponse.status >= 400) {
          return new Response(`Upstream Error: ${upstreamResponse.status}`, {
            status: upstreamResponse.status,
            headers: CORS_HEADERS,
          });
        }

        const contentType = (upstreamResponse.headers.get("Content-Type") || "").toLowerCase();
        const isHls = targetUrl.endsWith(".m3u8") || targetUrl.includes(".m3u8?") || targetUrl.endsWith(".txt") || contentType.includes("mpegurl");

        // 3. معالجة قوائم m3u8 وإعادة كتابة الروابط
        if (isHls) {
          const rawText = await upstreamResponse.text();
          if (rawText.trim().startsWith("#EXTM3U")) {
            const modified = rewritePlaylist(rawText, targetUrl, referer, url.origin);
            return new Response(modified, {
              status: 200,
              headers: {
                "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                ...CORS_HEADERS,
              },
            });
          }
        }

        // 4. تمرير مقاطع الفيديو الثنائية (.ts / .mp4) بتدفق مباشر
        const responseHeaders = new Headers(CORS_HEADERS);
        responseHeaders.set("Content-Type", contentType || (targetUrl.includes(".ts") ? "video/mp2t" : "application/octet-stream"));

        // نقل ترويسات الأجزاء (Range / Content-Length)
        for (const h of ["Content-Length", "Content-Range", "Accept-Ranges", "Content-Disposition"]) {
          const val = upstreamResponse.headers.get(h);
          if (val) responseHeaders.set(h, val);
        }

        return new Response(upstreamResponse.body, {
          status: upstreamResponse.status,
          headers: responseHeaders,
        });

      } catch (err) {
        return new Response(`Worker Proxy Gateway Error: ${err.message}`, {
          status: 502,
          headers: CORS_HEADERS,
        });
      }
    }

    return new Response("Not Found", { status: 404, headers: CORS_HEADERS });
  },
};
