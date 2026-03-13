import aiohttp
import asyncio
from typing import List, Dict, Any, Optional
import re

# Platform-specific dead message patterns
PATTERNS = {
    "quark": ["分享已失效", "分享链接已失效", "资源已失效", "文件已失效", "该分享已过期", "分享已删除"],
    "baidu": ["分享人已取消分享", "页面不存在了", "链接不存在", "分享的文件已被取消", "分享链接已失效", "给出的链接无效", "啊哦，你所访问的页面不存在了"],
    "aliyun": ["该分享已过期", "分享已取消", "链接不存在", "已被取消分享", "保存在云端的链接已过期"],
    "common": ["失效", "不存在", "取消", "删除", "过期", "404", "无效"]
}

class LinkValidator:
    def __init__(self, proxy: str = None):
        self.proxy = proxy
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    async def check_link(self, url: str) -> bool:
        """Return True if link is likely valid, False if dead."""
        try:
            platform = "common"
            if "pan.quark.cn" in url:
                platform = "quark"
            elif "pan.baidu.com" in url:
                platform = "baidu"
            elif "aliyundrive.com" in url or "alipan.com" in url:
                platform = "aliyun"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, proxy=self.proxy, timeout=6) as response:
                    if response.status == 404:
                        return False
                    
                    # For Baidu, sometimes 403/405 is just anti-bot on mobile headers, 
                    # but usually 200 is what we want.
                    if response.status >= 400 and platform != "baidu":
                        return False
                    
                    text = await response.text()
                    
                    # Pattern check
                    for p in PATTERNS.get(platform, PATTERNS["common"]):
                        if p in text:
                            return False
                    
                    # Quark specific: invalid links often have share_id as null/empty in the JSON config
                    if platform == "quark":
                        if '"share_id":""' in text or '"share_id":null' in text or '"title":""' in text:
                            # But wait, title="" might be valid for some folders. 
                            # Let's check share_id more strictly.
                            if '分享已失效' in text or '分享链接已失效' in text:
                                return False
                            # If we see the "expired" image URL but no evidence of success
                            if 'resource/202404/40dcf700-fdf2' in text and '"stared":false' not in text:
                                # This is a bit heuristic, let's stick to text patterns mainly.
                                pass

                    return True
        except Exception as e:
            # print(f"[Validator] Error checking {url}: {e}")
            return False

    async def filter_links(self, links: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Validate a list of links concurrently and return only valid ones."""
        if not links:
            return []
        semaphore = asyncio.Semaphore(5)
        async def sem_check(link):
            async with semaphore:
                return await self.check_link(link['url'])
        tasks = [sem_check(l) for l in links]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [links[i] for i, is_valid in enumerate(results) if is_valid is True]

link_validator = LinkValidator()
