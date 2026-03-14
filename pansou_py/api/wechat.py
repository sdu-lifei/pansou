import hashlib
import time
import asyncio
import xml.etree.ElementTree as ET
from typing import Optional
from fastapi import APIRouter, Request, BackgroundTasks, Query, Response
from pansou_py.core.config import settings
from pansou_py.core.cache import cache_service
from pansou_py.core.search import search_service
from pansou_py.utils.validator import link_validator

# Configure validator with proxy if available
link_validator.proxy = settings.PROXY

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
    """Format search results into WeChat-friendly text."""
    merged = results_data.get("merged_by_type", {})
    total = results_data.get("total", 0)

    if total == 0 or not merged:
        return f"😔 未找到「{keyword}」相关资源\n\n💡 试试：完整名称、英文名或年份"

    lines = [f"🔍「{keyword}」找到 {total} 条结果\n"]
    count = 0

    for disk_type, links in merged.items():
        for item in links:
            if count >= 10:
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
        lines.append(f"注：仅显示验证有效的最近 10 条结果")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Background search task (Silent caching)
# ──────────────────────────────────────────────────────────────────────────────

async def _do_search_and_cache(openid: str, keyword: str):
    """Background task to fetch more results and cache them silently."""
    try:
        # Deep search: 5 pages
        result = await search_service.search(keyword=keyword, max_pages=5)
        if result.get("merged_by_type"):
            all_links = []
            for t_links in result["merged_by_type"].values():
                all_links.extend(t_links)
            all_links.sort(key=lambda x: x.get("datetime", ""), reverse=True)
            
            # Validate top 12 links in background
            top_to_validate = all_links[:12]
            valid_ones = await link_validator.filter_links(top_to_validate, timeout=8)
            
            validated_set = {l['url'] for l in valid_ones}
            new_merged = {}
            for t_key, t_links in result["merged_by_type"].items():
                new_l = [l for l in t_links if l['url'] in validated_set]
                if new_l: new_merged[t_key] = new_l
            result["merged_by_type"] = new_merged
            result["total"] = sum(len(l) for l in new_merged.values())

        # Store result so next query is instant
        cache_service.set(f"wx_{openid}", {"keyword": keyword, "data": result}, ttl=1800)
    except:
        pass


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
    if not settings.WECHAT_TOKEN:
        return Response(content="Missing Config")
    if _verify_signature(signature, timestamp, nonce):
        return Response(content=echostr, media_type="text/plain")
    return Response(content="Forbidden", status_code=403)


@router.post("/wechat")
async def wechat_message(request: Request, background_tasks: BackgroundTasks):
    """Handle WeChat messages synchronously with high priority."""
    if not settings.WECHAT_TOKEN: return Response(content="")

    body = await request.body()
    params = dict(request.query_params)
    if not _verify_signature(params.get("signature", ""), params.get("timestamp", ""), params.get("nonce", "")):
        return Response(content="", status_code=403)

    try:
        msg = _parse_xml(body)
    except:
        return Response(content="")

    msg_type = msg.get("MsgType", "")
    openid = msg.get("FromUserName", "")
    gh_id = msg.get("ToUserName", "")

    if msg_type != "text":
        reply = "📢 请发送资源名称进行搜索，例如：庆余年"
        return Response(content=_build_text_reply(openid, gh_id, reply), media_type="application/xml")

    content = msg.get("Content", "").strip()
    
    # ── Command Handling ──────────────────────────────────────────────────────
    if content in ["结果", "查询", "result", "r", "查"]:
        cached = cache_service.get(f"wx_{openid}")
        if cached and cached.get("data"):
            reply = _format_results(cached["data"], cached.get("keyword", ""))
        else:
            reply = "⚠️ 暂时没搜到结果，请过几秒回复「查询」试试，或者重新发送资源名。"
        return Response(content=_build_text_reply(openid, gh_id, reply), media_type="application/xml")

    # ── Search Handling (Synchronous Priority) ────────────────────────────────
    keyword = content
    # Check cache first
    existing = cache_service.get(f"wx_{openid}")
    if existing and existing.get("keyword") == keyword and existing.get("data"):
        reply = _format_results(existing["data"], keyword)
        return Response(content=_build_text_reply(openid, gh_id, reply), media_type="application/xml")

    # Start search process
    async def get_results():
        # Fast search (1 page, harvesting logic inside SearchService)
        data = await search_service.search(keyword=keyword, max_pages=1)
        if data.get("merged_by_type"):
            all_links = []
            for t_links in data["merged_by_type"].values():
                all_links.extend(t_links)
            all_links.sort(key=lambda x: x.get("datetime", ""), reverse=True)
            
            # Ultra-fast validation for top 3
            top_3 = all_links[:3]
            try:
                valid_ones = await link_validator.filter_links(top_3, timeout=1.5)
                if valid_ones:
                    v_urls = {l['url'] for l in valid_ones}
                    new_merged = {}
                    for k, v in data["merged_by_type"].items():
                        match_l = [l for l in v if l['url'] in v_urls]
                        if match_l: new_merged[k] = match_l
                    data["merged_by_type"] = new_merged
                    data["total"] = sum(len(l) for l in new_merged.values())
            except:
                pass 
        return data

    try:
        # Wait up to 4.75 seconds to catch the WeChat deadline
        results_data = await asyncio.wait_for(get_results(), timeout=4.75)
        
        if results_data.get("total", 0) > 0:
            cache_service.set(f"wx_{openid}", {"keyword": keyword, "data": results_data}, ttl=1800)
            reply = _format_results(results_data, keyword)
            # Silent deep search in background to enrich cache
            background_tasks.add_task(_do_search_and_cache, openid, keyword)
        else:
            # Reached here but no results found in time
            reply = f"😔 未搜到「{keyword}」，后台正在努力搜寻中...\n\n👉 请过 10 秒后回复「查询」试试。"
            background_tasks.add_task(_do_search_and_cache, openid, keyword)
            
    except asyncio.TimeoutError:
        # Search timed out, send a nice failure message
        reply = f"⏳ 搜索「{keyword}」超时，请过几秒回复「查询」试试。\n\n💡 可能是搜索频道较多，正在努力抓取中。"
        background_tasks.add_task(_do_search_and_cache, openid, keyword)
    except Exception as e:
        reply = f"⚠️ 搜「{keyword}」时出错了，请稍后再试。"

    return Response(content=_build_text_reply(openid, gh_id, reply), media_type="application/xml")
