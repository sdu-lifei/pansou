import asyncio
from typing import List, Dict, Optional
from pansou_py.models.schemas import SearchResult
from pansou_py.core.cache import cache_service
from pansou_py.plugins import plugin_manager
from pansou_py.core.tg_searcher import telegram_searcher
from pansou_py.core.config import settings

class SearchService:
    def __init__(self):
        self.plugin_manager = plugin_manager

    def _merge_results(self, tg: List[SearchResult], plugin: List[SearchResult]) -> List[SearchResult]:
        seen = {}
        for r in tg + plugin:
            key = f"{r.channel}_{r.message_id}"
            if key not in seen:
                seen[key] = r
        merged = list(seen.values())
        merged.sort(key=lambda x: x.datetime, reverse=True)
        return merged

    async def search_plugins(self, keyword: str, plugins_filter: Optional[List[str]]) -> List[SearchResult]:
        plugins = self.plugin_manager.get_plugins()
        if plugins_filter:
            plugins = [p for p in plugins if p.name in plugins_filter]
        results_list = await asyncio.gather(*[p.search(keyword) for p in plugins], return_exceptions=True)
        return [r for res in results_list if isinstance(res, list) for r in res]

    async def search(
        self,
        keyword: str,
        channels: Optional[List[str]] = None,
        force_refresh: bool = False,
        res_type: str = "merge",
        src: str = "all",
        plugins: Optional[List[str]] = None,
        cloud_types: Optional[List[str]] = None,
    ) -> dict:
        cache_key = f"search_{keyword}_{src}_{plugins}"
        if not force_refresh:
            cached = cache_service.get(cache_key)
            if cached:
                return cached

        tg_results: List[SearchResult] = []
        plugin_results: List[SearchResult] = []

        channels_to_search = channels if channels else settings.default_channels

        if src in ["all", "tg"]:
            tg_list = await asyncio.gather(
                *[telegram_searcher.search(ch, keyword) for ch in channels_to_search],
                return_exceptions=True
            )
            for res in tg_list:
                if isinstance(res, list):
                    tg_results.extend(res)

        if src in ["all", "plugin"]:
            plugin_results = await self.search_plugins(keyword, plugins)

        all_results = self._merge_results(tg_results, plugin_results)

        merged_by_type: Dict = {}
        for r in all_results:
            for link in r.links:
                if cloud_types and link.type not in cloud_types:
                    continue
                merged_by_type.setdefault(link.type, []).append({
                    "url": link.url,
                    "password": link.password,
                    "note": r.title,
                    "datetime": r.datetime,
                    "source": f"tg:{r.channel}",
                    "images": r.images
                })

        response = {
            "total": len(all_results),
            **({"results": [r.model_dump() for r in all_results]} if res_type in ["all", "results"] else {}),
            **({"merged_by_type": merged_by_type} if res_type in ["all", "merge"] else {}),
        }

        cache_service.set(cache_key, response)
        return response

search_service = SearchService()
