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
    try:
        result = await search_service.search(keyword=keyword)
        # Store under openid so user can retrieve with "结果"
        cache_service.set(f"wx_{openid}", {"keyword": keyword, "data": result}, ttl=1800)
    except Exception as e:
        print(f"[WeChat BG Search] Error for {openid}/{keyword}: {e}")
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
    if not settings.WECHAT_TOKEN:
        return Response(content="")

    body = await request.body()

    # Verify signature
    params = dict(request.query_params)
    if not _verify_signature(
        params.get("signature", ""),
        params.get("timestamp", ""),
        params.get("nonce", ""),
    ):
        return Response(content="", status_code=403)

    msg = _parse_xml(body)
    msg_type = msg.get("MsgType", "")
    openid = msg.get("FromUserName", "")
    gh_id = msg.get("ToUserName", "")

    if msg_type != "text":
        reply = "📢 请发送文字消息搜索，例如：水浒传"
        return Response(content=_build_text_reply(openid, gh_id, reply), media_type="application/xml")

    content = msg.get("Content", "").strip()

    # ── Command: 结果 / 查询 ──────────────────────────────────────────────────
    if content in ["结果", "查询", "result", "r", "查"]:
        cached = cache_service.get(f"wx_{openid}")
        if not cached:
            reply = "⚠️ 暂无搜索结果，请先被发送资源名称"
        elif cached.get("data") is None:
            reply = f"⚠️ 搜索「{cached.get('keyword','')}」时出错，请重试"
        else:
            reply = _format_results(cached["data"], cached.get("keyword", ""))
        return Response(content=_build_text_reply(openid, gh_id, reply), media_type="application/xml")

    # ── Command: 帮助 ─────────────────────────────────────────────────────────
    if content in ["帮助", "help", "?"]:
        reply = (
            "🔍 PanSou 网盘资源搜索\n\n"
            "使用方法：\n"
            "1️⃣ 直接发送资源名称，如：水浒传\n"
            "2️⃣ 等待约 30 秒后发送：结果\n"
            "3️⃣ 即可看到搜索结果\n\n"
            "支持：百度网盘 夸克 阿里云盘 UC 迅雷等"
        )
        return Response(content=_build_text_reply(openid, gh_id, reply), media_type="application/xml")

    # ── Search ────────────────────────────────────────────────────────────────
    keyword = content
    # Check if already cached
    cached = cache_service.get(f"wx_{openid}_kw")
    if cached == keyword:
        existing = cache_service.get(f"wx_{openid}")
        if existing and existing.get("data"):
            reply = _format_results(existing["data"], keyword)
            return Response(content=_build_text_reply(openid, gh_id, reply), media_type="application/xml")

    # Start background search; remember keyword for this user
    cache_service.set(f"wx_{openid}_kw", keyword, ttl=1800)
    background_tasks.add_task(_do_search_and_cache, openid, keyword)

    reply = (
        f"⏳ 正在搜索「{keyword}」\n"
        f"约 30 秒后请发送：结果\n\n"
        f'💡 发送"帮助"查看使用说明'
    )
    return Response(content=_build_text_reply(openid, gh_id, reply), media_type="application/xml")
