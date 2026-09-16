import aiohttp

# 资料库查询超时（秒）——避免第三方服务无响应时请求无限挂起
LIBRARY_TIMEOUT = 10


async def fetch_library_info(
    url: str,
    *,
    target_id: str,
    cookies: str = "",
) -> dict[str, str]:
    """Fetch library data for a QQ user."""
    headers = {
        "accept": "*/*",
        "referer": url,
        "user-agent": "Mozilla/5.0",
    }
    if cookies:
        headers["Cookie"] = cookies
    # 修复BUG：显式设置超时，避免请求无限挂起
    timeout = aiohttp.ClientTimeout(total=LIBRARY_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        resp = await session.get(
            url=f"{url}/api/query",
            params={"value": target_id},
            headers=headers,
        )
        resp.raise_for_status()
        payload = await resp.json(content_type=None)
        data = payload.get("data")
        if not data:
            raise ValueError(f"library returned empty data for {target_id=}")

        return data
