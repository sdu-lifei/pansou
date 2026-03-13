import hashlib
import time
import asyncio
import xml.etree.ElementTree as ET
from typing import Optional
from fastapi import APIRouter, Request, BackgroundTasks, Query, Response
from pansou_py.core.config import settings
from pansou_py.core.cache import cache_service
from pansou_py.core.search import search_service

router = APIRouter()

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _verify_signature(signature: str, timestamp: str, nonce: str) -> bool:
    """Verify WeChat webhook signature."""
    items = sorted([settings.WECHAT_TOKEN, timestamp, nonce])
    sha1 = hashlib.sha1("".join(items).encode()).hexdigest()
    return sha1 == signature


def _parse_xml(body: bytes) -> dict:
    """Parse WeChat XML message into dict."""
    root = ET.fromstring(body)
    return {child.tag: (child.text or "") for child in root}


def _build_text_reply(to_user: str, from_user: str, content: str) -> str:
    """Build WeChat text reply XML."""
    ts = int(time.time())
    return (
        f"<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{ts}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"</xml>"
    )


def _format_results(results_data: dict, keyword: str) -> str:
    """Format search results into WeChat-friendly text (1500 char limit safe)."""
    merged = results_data.get("merged_by_type", {})
    total = results_data.get("total", 0)

    if total == 0 or not merged:
        return f"😔 未找到「{keyword}」相关资源\n\n💡 试试：完整名称、英文名或年份+类型"

    lines = [f"🔍「{keyword}」找到 {total} 条结果\n"]
    count = 0

    for disk_type, links in merged.items():
        for item in links:
            if count >= 10:  # WeChat message length limit friendly
                break
            count += 1
            note = item.get("note", "")
            url = item.get("url", "")
            pwd = item.get("password", "")
            icon = {
                "baidu": "🔵", "quark": "🟠", "aliyun": "🟢",
                "uc": "🟣", "xunlei": "⚡", "123": "🔴",
            }.get(disk_type, "📦")

            lines.append(f"{count}. {note}")
            lines.append(f"  {icon} {disk_type}网盘: {url}")
            if pwd:
                lines.append(f"  🔑 密码: {pwd}")
            lines.append("")

    if total > 10:
        lines.append(f'... 还有 {total - 10} 条，发"更多"查看')

    lines.append("💡 直接发资源名搜索新内容")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Background search task
# ──────────────────────────────────────────────────────────────────────────────

async def _do_search_and_cache(openid: str, keyword: str):
    """Perform search in background and store result keyed by openid."""
    print(f"🚀 [WeChat BG] Starting background search for '{keyword}' ({openid})")
    try:
        startTime = time.time()
        result = await search_service.search(keyword=keyword)
        duration = time.time() - startTime
        print(f"✅ [WeChat BG] Search completed in {duration:.2f}s, total results: {result.get('total', 0)}")
        
        # Store under openid so user can retrieve with "结果"
        cache_service.set(f"wx_{openid}", {"keyword": keyword, "data": result}, ttl=1800)
        print(f"💾 [WeChat BG] Results cached for '{openid}'")
    except Exception as e:
        import traceback
        print(f"❌ [WeChat BG] Search error for {openid}/{keyword}: {e}")
        traceback.print_exc()
        cache_service.set(f"wx_{openid}", {"keyword": keyword, "data": None}, ttl=300)


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/wechat")
async def wechat_verify(
    signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
):
    """WeChat server URL verification."""
    if not settings.WECHAT_TOKEN:
        return Response(content="WeChat not configured", status_code=500, media_type="text/plain")
    if _verify_signature(signature, timestamp, nonce):
        return Response(content=echostr, media_type="text/plain")
    return Response(content="Invalid signature", status_code=403, media_type="text/plain")


@router.post("/wechat")
async def wechat_message(request: Request, background_tasks: BackgroundTasks):
    """Handle incoming WeChat messages."""
    print("\n" + "="*50)
    print("▶️ [WeChat] Received POST request")
    
    if not settings.WECHAT_TOKEN:
        print("❌ [WeChat] Error: WECHAT_TOKEN is not configured!")
        return Response(content="")

    body = await request.body()
    print(f"📦 [WeChat] Raw Body:\n{body.decode('utf-8', errors='ignore')}")

    # Verify signature
    params = dict(request.query_params)
    print(f"🔑 [WeChat] Query Params: {params}")
    
    if not _verify_signature(
        params.get("signature", ""),
        params.get("timestamp", ""),
        params.get("nonce", ""),
    ):
        print("❌ [WeChat] Error: Signature verification failed!")
        return Response(content="", status_code=403)

    print("✅ [WeChat] Signature verified successfully.")
    
    try:
        msg = _parse_xml(body)
        print(f"📄 [WeChat] Parsed XML: {msg}")
    except Exception as e:
        print(f"❌ [WeChat] Error parsing XML: {e}")
        return Response(content="")

    msg_type = msg.get("MsgType", "")
    openid = msg.get("FromUserName", "")
    gh_id = msg.get("ToUserName", "")

    if msg_type != "text":
        print(f"⚠️ [WeChat] Non-text message type received: {msg_type}")
        reply = "📢 请发送文字消息搜索，例如：水浒传"
        resp_xml = _build_text_reply(openid, gh_id, reply)
        print(f"📤 [WeChat] Replying XML:\n{resp_xml}")
        return Response(content=resp_xml, media_type="application/xml")

    content = msg.get("Content", "").strip()
    print(f"💬 [WeChat] User '{openid}' sent text: '{content}'")

    resp_xml = ""

    # ── Command: 结果 / 查询 ──────────────────────────────────────────────────
    if content in ["结果", "查询", "result", "r", "查"]:
        cached = cache_service.get(f"wx_{openid}")
        if not cached:
            # Check if there is even a pending keyword
            pending_kw = cache_service.get(f"wx_{openid}_kw")
            if pending_kw:
                reply = (
                    f"⌛ 正在为您努力抓取「{pending_kw}」中...\n\n"
                    f"抓取 Telegram 频道资源通常需要约 20-40 秒的时间。\n"
                    f"👉 请再稍等片刻后回复：结果"
                )
            else:
                reply = "⚠️ 您还没有发送过查询关键词哦。\n\n💡 请直接发送您想找的资源名称，例如：水浒传"
        elif cached.get("data") is None:
            reply = f"⚠️ 搜索「{cached.get('keyword','')}」时出错，请换个词重试"
        else:
            reply = _format_results(cached["data"], cached.get("keyword", ""))
        resp_xml = _build_text_reply(openid, gh_id, reply)

    # ── Command: 帮助 ─────────────────────────────────────────────────────────
    elif content in ["帮助", "help", "?"]:
        reply = (
            "🔍 PanSou 网盘资源搜索\n\n"
            "使用方法：\n"
            "1️⃣ 直接发送资源名称，如：水浒传\n"
            "2️⃣ 等待约 30 秒后发送：结果\n"
            "3️⃣ 即可看到搜索结果\n\n"
            "支持：百度网盘 夸克 阿里云盘 UC 迅雷等"
        )
        resp_xml = _build_text_reply(openid, gh_id, reply)

    # ── Search ────────────────────────────────────────────────────────────────
    else:
        keyword = content
        # Check if already cached
        cached_kw = cache_service.get(f"wx_{openid}_kw")
        if cached_kw == keyword:
            existing = cache_service.get(f"wx_{openid}")
            if existing and existing.get("data"):
                reply = _format_results(existing["data"], keyword)
                resp_xml = _build_text_reply(openid, gh_id, reply)
        
        if not resp_xml:
            # Try synchronous search with a timeout (e.g., 4s) to fit WeChat's 5s window
            print(f"🔎 [WeChat] Attempting sync search for '{keyword}'...")
            cache_service.set(f"wx_{openid}_kw", keyword, ttl=1800)
            
            try:
                # Use asyncio.wait_for to limit search time
                results_data = await asyncio.wait_for(search_service.search(keyword=keyword), timeout=4.0)
                print(f"✅ [WeChat] Sync search finished in time. Results: {results_data.get('total', 0)}")
                
                # Cache and format reply
                cache_service.set(f"wx_{openid}", {"keyword": keyword, "data": results_data}, ttl=1800)
                reply = _format_results(results_data, keyword)
                resp_xml = _build_text_reply(openid, gh_id, reply)
            except asyncio.TimeoutError:
                print(f"⏳ [WeChat] Sync search timed out (>4s). Falling back to background task.")
                # Search too slow, fall back to background + "results" command
                background_tasks.add_task(_do_search_and_cache, openid, keyword)
                reply = (
                    f"⏳ 正在努力搜索「{keyword}」...\n\n"
                    f"因为去好几个频道抓取需要一点时间，\n"
                    f"👉 请在【约 20-30 秒后】回复：结果\n\n"
                    f"即可查看搜索内容！"
                )
                resp_xml = _build_text_reply(openid, gh_id, reply)
            except Exception as e:
                print(f"❌ [WeChat] Sync search failed: {e}")
                reply = f"⚠️ 搜索「{keyword}」时出错了，请稍后再试。"
                resp_xml = _build_text_reply(openid, gh_id, reply)
    
    print(f"📤 [WeChat] Replying XML:\n{resp_xml}")
    return Response(content=resp_xml, media_type="application/xml")
