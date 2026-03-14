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
        lines.append(f"注：只显示验证有效的最近 10 条结果")

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
        # Deep search: fetch 5 pages per channel
        result = await search_service.search(keyword=keyword, max_pages=5)
        
        # Validating top 15 links (concurrently) before caching
        if result.get("merged_by_type"):
            all_links = []
            for t_links in result["merged_by_type"].values():
                all_links.extend(t_links)
            
            # Sort by date
            all_links.sort(key=lambda x: x.get("datetime", ""), reverse=True)
            
            top_to_validate = all_links[:15]
            print(f"🕵️ [WeChat BG] Validating top {len(top_to_validate)} links...")
            valid_ones = await link_validator.filter_links(top_to_validate, timeout=6)
            print(f"✅ [WeChat BG] Validation done. {len(valid_ones)}/{len(top_to_validate)} valid.")
            
            validated_set = {l['url'] for l in valid_ones}
            new_merged = {}
            for t_key, t_links in result["merged_by_type"].items():
                new_l = [l for l in t_links if l['url'] in validated_set]
                if new_l:
                    new_merged[t_key] = new_l
            result["merged_by_type"] = new_merged
            result["total"] = sum(len(l) for l in new_merged.values())

        duration = time.time() - startTime
        print(f"✅ [WeChat BG] Search + Validation completed in {duration:.2f}s, total valid: {result.get('total', 0)}")
        
        # Store under openid so user can retrieve with "查询"
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
    # Verify signature
    params = dict(request.query_params)
    if not _verify_signature(
        params.get("signature", ""),
        params.get("timestamp", ""),
        params.get("nonce", ""),
    ):
        print("❌ [WeChat] Error: Signature verification failed!")
        return Response(content="", status_code=403)

    try:
        msg = _parse_xml(body)
    except Exception as e:
        print(f"❌ [WeChat] Error parsing XML: {e}")
        return Response(content="")

    msg_type = msg.get("MsgType", "")
    openid = msg.get("FromUserName", "")
    gh_id = msg.get("ToUserName", "")

    if msg_type != "text":
        reply = "📢 请发送文字消息搜索，例如：庆余年"
        resp_xml = _build_text_reply(openid, gh_id, reply)
        return Response(content=resp_xml, media_type="application/xml")

    content = msg.get("Content", "").strip()
    print(f"💬 [WeChat] User '{openid}' sent text: '{content}'")

    resp_xml = ""

    # ── Command: 查询 / 结果 ──────────────────────────────────────────────────
    if content in ["结果", "查询", "result", "r", "查"]:
        cached = cache_service.get(f"wx_{openid}")
        if not cached:
            pending_kw = cache_service.get(f"wx_{openid}_kw")
            if pending_kw:
                reply = (f"⌛ 正在为您搜「{pending_kw}」中...\n"
                         f"👉 请再过几秒回复：查询")
            else:
                reply = "⚠️ 您还没有发送过查询关键词哦。\n\n💡 直接发送资源名称，例如：庆余年"
        elif cached.get("data") is None:
            reply = f"⚠️ 搜索「{cached.get('keyword','')}」时出错，请换个词重试"
        else:
            reply = _format_results(cached["data"], cached.get("keyword", ""))
        resp_xml = _build_text_reply(openid, gh_id, reply)

    # ── Command: 帮助 ─────────────────────────────────────────────────────────
    elif content in ["帮助", "help", "?", "🔍"]:
        reply = (
            "🔍 PanSou 网盘资源搜索\n\n"
            "使用方法：\n"
            "1️⃣ 直接发送资源名称，如：庆余年\n"
            "2️⃣ 有结果会直接回复（通常 5s 内）\n"
            "3️⃣ 如果没立刻出，等 10-20s 后回复：查询\n\n"
            "集成频道：Lsp115, Aliyun_4K_Movies 等多源"
        )
        resp_xml = _build_text_reply(openid, gh_id, reply)

    # ── Search ────────────────────────────────────────────────────────────────
    else:
        keyword = content
        # Check cache
        cached_kw = cache_service.get(f"wx_{openid}_kw")
        if cached_kw == keyword:
            existing = cache_service.get(f"wx_{openid}")
            if existing and existing.get("data"):
                reply = _format_results(existing["data"], keyword)
                resp_xml = _build_text_reply(openid, gh_id, reply)
        
        if not resp_xml:
            cache_service.set(f"wx_{openid}_kw", keyword, ttl=1800)
            
            async def fast_search_and_validate():
                # Shallow search (1 page)
                data = await search_service.search(keyword=keyword, max_pages=1)
                if data.get("merged_by_type"):
                    all_links = []
                    for t_links in data["merged_by_type"].values():
                        all_links.extend(t_links)
                    all_links.sort(key=lambda x: x.get("datetime", ""), reverse=True)
                    
                    top_to_validate = all_links[:6]
                    # Fast validation (2s timeout)
                    valid_ones = await link_validator.filter_links(top_to_validate, timeout=2)
                    
                    val_urls = {l['url'] for l in valid_ones}
                    new_merged = {}
                    for k, v in data["merged_by_type"].items():
                        match_l = [l for l in v if l['url'] in val_urls]
                        if match_l:
                            new_merged[k] = match_l
                    data["merged_by_type"] = new_merged
                    data["total"] = sum(len(l) for l in new_merged.values())
                return data

            try:
                print(f"🔎 [WeChat] Attempting sync search (3.5s limit) for '{keyword}'...")
                results_data = await asyncio.wait_for(fast_search_and_validate(), timeout=3.5)
                
                print(f"✅ [WeChat] Sync search finished. Valid: {results_data.get('total', 0)}")
                cache_service.set(f"wx_{openid}", {"keyword": keyword, "data": results_data}, ttl=1800)
                reply = _format_results(results_data, keyword)
                resp_xml = _build_text_reply(openid, gh_id, reply)
                # Background: start a deep search (5 pages) to enrich cache
                background_tasks.add_task(_do_search_and_cache, openid, keyword)
            except (asyncio.TimeoutError, Exception) as e:
                print(f"⏳ [WeChat] Sync search fallback ({type(e).__name__}): {e}")
                background_tasks.add_task(_do_search_and_cache, openid, keyword)
                reply = (
                    f"⏳ 正在抓取「{keyword}」，请过几秒直接回复：查询\n\n"
                    f"正在多频道搜索并验证链接有效性..."
                )
                resp_xml = _build_text_reply(openid, gh_id, reply)
    
    if not resp_xml:
        resp_xml = _build_text_reply(openid, gh_id, "⚠️ 系统繁忙，请重试")

    print(f"📤 [WeChat] Replying XML:\n{resp_xml}")
    return Response(content=resp_xml, media_type="application/xml")
