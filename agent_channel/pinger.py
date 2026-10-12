import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

# 仅基于 UDP/QUIC 的节点类型，无 TCP 监听，不适用于 TCP 握手测速。
# 注：anytls 不在其中——实测它是 TCP+TLS（内核 trace 为 TCP 拨号、零 UDP），
# 曾经误列会把它从排序里跳过并排到最后。
UDP_ONLY_TYPES = {"hysteria2", "tuic"}


def ping_node(node: dict[str, Any], timeout: float = 1.5) -> float | None:
    """通过建立 TCP 握手测量节点延迟 (毫秒)"""
    host = node.get("server")
    port = node.get("server_port")
    if not host or not port:
        return None
    start = time.perf_counter()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return (time.perf_counter() - start) * 1000.0
    except (TimeoutError, OSError):
        return None


def rank_nodes(
    nodes: list[dict[str, Any]], timeout: float = 1.5
) -> list[dict[str, Any]]:
    """按 TCP 握手延迟升序返回节点列表，供调用方逐个验证真实转发。

    UDP 型节点（hysteria2/tuic）无 TCP 监听，跳过 TCP 测速排到末尾；
    CDN 节点 (argo/cloudflare) 延迟偏高且常带 502 隧道失效，故排序靠后；
    无法建立 TCP 握手的节点排到最后（仍保留，避免仅因探测抖动而丢失节点）。
    """
    if not nodes:
        return []

    tcp_nodes = [n for n in nodes if n.get("type") not in UDP_ONLY_TYPES]
    results: list[tuple[float, dict[str, Any]]] = []
    if tcp_nodes:
        with ThreadPoolExecutor(max_workers=min(len(tcp_nodes), 5)) as pool:
            future_map = {pool.submit(ping_node, n, timeout): n for n in tcp_nodes}
            for fut in as_completed(future_map):
                node = future_map[fut]
                try:
                    rtt = fut.result()
                    if rtt is not None:
                        results.append((rtt, node))
                except Exception:
                    # ping_node 已内部捕获网络异常，此处兜底；该节点会在下方 fallback 中排到末尾
                    continue

    def sort_key(item: tuple[float, dict[str, Any]]):
        rtt, node = item
        tag = node.get("tag", "").lower()
        server = node.get("server", "").lower()
        is_cdn = "argo" in tag or "cloudflare" in server
        return (1 if is_cdn else 0, rtt)

    results.sort(key=sort_key)
    ranked = [node for _, node in results]
    ranked_ids = {id(node) for _, node in results}
    for node in nodes:
        if id(node) not in ranked_ids:
            ranked.append(node)
    return ranked
