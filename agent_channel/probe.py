"""HTTP 探测工具：测试目标端点经由代理的连通性与握手延迟。

独立于 UI 层，供控制面板与 Agent 工具共用。
"""

import time
import urllib.error
import urllib.request
from typing import Any

from .utils import MAX_PROBE_BYTES, read_limited


def test_single_endpoint(
    url: str, proxy_url: str | None = None, timeout: float = 3.5
) -> dict[str, Any]:
    """通过指定代理测试目标 URL 的连通性与握手延迟。

    返回 `{reachable, status, latency_ms, via}`；失败时返回
    `{reachable: False, error, via}`。

    `via` 是 `"channel"`（走了通道代理）或 `"direct"`（直连）—— **结果必须自带这次
    到底走的是哪条路**，否则“通道没跑时的直连结果”会被读成“通道没问题”。

    HTTP 4xx/5xx 视为链路可达（服务端已应答）。
    """
    start = time.perf_counter()
    via = "channel" if proxy_url else "direct"
    try:
        # 直连就显式直连：`build_opener()` 不带 handler 时会挂上 urllib 默认的
        # ProxyHandler，于是**读宿主环境的 HTTP_PROXY / HTTPS_PROXY** —— 那样测的是
        # 宿主代理，不是通道，也不是真直连。
        if proxy_url:
            proxy_handler = urllib.request.ProxyHandler(
                {"http": proxy_url, "https": proxy_url}
            )
        else:
            proxy_handler = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request(
            url, headers={"User-Agent": "agent-channel-tester"}
        )
        with opener.open(req, timeout=timeout) as resp:
            latency = (time.perf_counter() - start) * 1000.0
            return {
                "reachable": True,
                "status": resp.status,
                "latency_ms": round(latency, 1),
                "via": via,
            }
    except urllib.error.HTTPError as e:
        latency = (time.perf_counter() - start) * 1000.0
        return {
            "reachable": True,
            "status": e.code,
            "latency_ms": round(latency, 1),
            "via": via,
        }
    except Exception as e:
        # 报完整信息，不要 split(":")[-1]：那会把 "HTTP Error 404: Not Found" 截成
        # "Not Found"，丢掉最该看的状态码。
        return {"reachable": False, "error": str(e).strip(), "via": via}


def geolocate_via_proxy(proxy_url: str, timeout: float = 12.0) -> dict[str, Any]:
    """通过通道代理探测落地 IP 的地理位置与天气。

    用 ipwho.is（https 免费，按出口 IP 返回城市/国家/ISP/坐标）+ Open-Meteo
    （https 免费，按坐标返回当前天气）。走代理后两个服务看到的都是节点出口 IP，
    从而真实反映“落地城市”，等于验证了 IP 物理地址。
    """
    import json

    def _fetch(url: str) -> str:
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        opener = urllib.request.build_opener(handler)
        req = urllib.request.Request(url, headers={"User-Agent": "agent-channel"})
        with opener.open(req, timeout=timeout) as resp:
            # 限长：这段流量经**不受信节点出口**，节点比订阅源更不可信
            return read_limited(resp, MAX_PROBE_BYTES).decode("utf-8", errors="replace")

    try:
        geo = json.loads(_fetch("https://ipwho.is/"))
    except Exception as e:
        return {"error": f"落地探测失败: {e}"}

    conn = geo.get("connection")
    result: dict[str, Any] = {
        "ip": geo.get("ip"),
        "city": geo.get("city"),
        "region": geo.get("region"),
        "country": geo.get("country"),
        "isp": conn.get("isp") if isinstance(conn, dict) else None,
    }
    lat = geo.get("latitude")
    lon = geo.get("longitude")
    if lat is not None and lon is not None:
        try:
            w = json.loads(
                _fetch(
                    f"https://api.open-meteo.com/v1/forecast?latitude={lat}"
                    f"&longitude={lon}&current_weather=true"
                )
            )
            cw = w.get("current_weather") or {}
            result["weather"] = {
                "temp_c": cw.get("temperature"),
                "code": cw.get("weathercode"),
            }
        except Exception:
            result["weather"] = None
    return result
