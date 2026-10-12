import base64
import json
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


def parse_vless_uri(uri: str) -> dict[str, Any]:
    """解析 vless:// 链接为 sing-box Outbound JSON 结构，支持 TLS/Reality 与 xtls-rprx-vision"""
    parts = urlsplit(uri)
    uuid_str = parts.username
    server = parts.hostname
    port = parts.port or 443
    query = parse_qs(parts.query)
    tag = unquote(parts.fragment) if parts.fragment else f"{server}:{port}"

    outbound: dict[str, Any] = {
        "type": "vless",
        "tag": tag,
        "server": server,
        "server_port": port,
        "uuid": uuid_str,
    }

    flow = query.get("flow", [""])[0]
    if flow:
        outbound["flow"] = flow

    security = query.get("security", [""])[0]
    if security in ("tls", "reality"):
        sni = query.get("sni", [server])[0]
        tls_config: dict[str, Any] = {
            "enabled": True,
            "server_name": sni,
            "insecure": False,
        }
        fp = query.get("fp", ["chrome"])[0]
        if fp:
            tls_config["utls"] = {"enabled": True, "fingerprint": fp}

        if security == "reality":
            pbk = query.get("pbk", [""])[0]
            sid = query.get("sid", [""])[0]
            tls_config["reality"] = {
                "enabled": True,
                "public_key": pbk,
                "short_id": sid,
            }
        outbound["tls"] = tls_config

    net_type = query.get("type", ["tcp"])[0]
    if net_type == "ws":
        host = query.get("host", [""])[0] or outbound.get("tls", {}).get(
            "server_name", server
        )
        outbound["transport"] = {
            "type": "ws",
            "path": query.get("path", ["/"])[0],
            "headers": {"Host": host},
        }
    return outbound


def parse_trojan_uri(uri: str) -> dict[str, Any]:
    parts = urlsplit(uri)
    password = parts.username
    server = parts.hostname
    port = parts.port or 443
    query = parse_qs(parts.query)
    tag = unquote(parts.fragment) if parts.fragment else f"{server}:{port}"

    return {
        "type": "trojan",
        "tag": tag,
        "server": server,
        "server_port": port,
        "password": password,
        "tls": {
            "enabled": True,
            "server_name": query.get("sni", [server])[0],
            "insecure": False,
        },
    }


def parse_ss_uri(uri: str) -> dict[str, Any] | None:
    parts = urlsplit(uri)
    tag = unquote(parts.fragment) if parts.fragment else "Shadowsocks"
    try:
        user_info = parts.username or ""
        if not parts.password and user_info:
            missing_padding = len(user_info) % 4
            if missing_padding:
                user_info += "=" * (4 - missing_padding)
            decoded = base64.b64decode(user_info).decode("utf-8")
            method, password = decoded.split(":", 1)
        else:
            method = user_info
            password = parts.password or ""

        return {
            "type": "shadowsocks",
            "tag": tag,
            "server": parts.hostname,
            "server_port": parts.port or 8388,
            "method": method,
            "password": password,
        }
    except Exception:
        return None


def parse_vmess_uri(uri: str) -> dict[str, Any] | None:
    """解析 v2rayN 的 vmess://（base64 JSON 形态）为 sing-box vmess outbound。

    只支持 base64 编码的 JSON（社区最通行）；不支持 XOR 变体与查询串形态。
    """
    raw = uri[len("vmess://") :].strip()
    try:
        data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    server = data.get("add")
    uuid = data.get("id")
    port_raw = data.get("port")
    if not server or not uuid or not port_raw:
        return None
    try:
        port = int(str(port_raw))
    except (TypeError, ValueError):
        return None
    tag = data.get("ps") or f"{server}:{port}"
    outbound: dict[str, Any] = {
        "type": "vmess",
        "tag": tag,
        "server": server,
        "server_port": port,
        "uuid": uuid,
        "security": data.get("scy") or "auto",
    }
    net = data.get("net") or "tcp"
    host = data.get("host") or ""
    if net == "ws":
        transport: dict[str, Any] = {"type": "ws", "path": data.get("path") or "/"}
        if host:
            transport["headers"] = {"Host": host}
        outbound["transport"] = transport
    elif net == "grpc":
        outbound["transport"] = {
            "type": "grpc",
            "service_name": data.get("path") or "",
        }
    elif net == "h2":
        transport = {"type": "http"}
        if host:
            transport["host"] = [host]
        outbound["transport"] = transport
    if (data.get("tls") or "").lower() in ("tls", "reality"):
        outbound["tls"] = {
            "enabled": True,
            "server_name": data.get("sni") or host or server,
            "insecure": False,
        }
    return outbound


def parse_hysteria2_uri(uri: str) -> dict[str, Any] | None:
    """解析 hysteria2:// 为 sing-box hysteria2 outbound。

    形态：hysteria2://[auth@]host[:port]/?sni=&insecure=&obfs=&obfs-password=#tag
    auth 即 password（user:pass 形态不支持）。hysteria2 走 QUIC，必须带 tls。
    """
    parts = urlsplit(uri)
    server = parts.hostname
    password = parts.username
    if not server or not password:
        return None
    port = parts.port or 443
    query = parse_qs(parts.query)
    tag = unquote(parts.fragment) if parts.fragment else f"{server}:{port}"
    sni = query.get("sni", [server])[0] or server
    insecure = query.get("insecure", [""])[0] in ("1", "true", "True")
    outbound: dict[str, Any] = {
        "type": "hysteria2",
        "tag": tag,
        "server": server,
        "server_port": port,
        "password": unquote(password),
        "tls": {"enabled": True, "server_name": sni, "insecure": insecure},
    }
    obfs = query.get("obfs", [""])[0]
    if obfs:
        outbound["obfs"] = {
            "type": obfs,
            "password": query.get("obfs-password", [""])[0],
        }
    return outbound


def parse_tuic_uri(uri: str) -> dict[str, Any] | None:
    """解析 tuic:// 为 sing-box tuic outbound。

    形态：tuic://<uuid>:<password>@host:port/?sni=&alpn=&congestion_control=#tag
    需要 uuid + password（v4 token 形态不支持）。tuic 走 QUIC，必须带 tls。
    """
    parts = urlsplit(uri)
    server = parts.hostname
    uuid_str = parts.username
    password = parts.password
    if not server or not uuid_str or not password:
        return None
    port = parts.port or 443
    query = parse_qs(parts.query)
    tag = unquote(parts.fragment) if parts.fragment else f"{server}:{port}"
    tls_config: dict[str, Any] = {
        "enabled": True,
        "server_name": query.get("sni", [server])[0] or server,
    }
    alpn = query.get("alpn", [""])[0]
    if alpn:
        tls_config["alpn"] = [a for a in alpn.split(",") if a]
    return {
        "type": "tuic",
        "tag": tag,
        "server": server,
        "server_port": port,
        "uuid": unquote(uuid_str),
        "password": unquote(password),
        "congestion_control": query.get("congestion_control", ["cubic"])[0] or "cubic",
        "tls": tls_config,
    }


def parse_node_uri(uri: str) -> dict[str, Any] | None:
    s = uri.strip()
    if s.startswith("vless://"):
        return parse_vless_uri(s)
    elif s.startswith("vmess://"):
        return parse_vmess_uri(s)
    elif s.startswith("trojan://"):
        return parse_trojan_uri(s)
    elif s.startswith("ss://"):
        return parse_ss_uri(s)
    elif s.startswith("hysteria2://"):
        return parse_hysteria2_uri(s)
    elif s.startswith("tuic://"):
        return parse_tuic_uri(s)
    return None


def extract_nodes_from_subscription(
    raw_text: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """智能解析订阅内容，返回 (nodes, errors)。

    1. 自动识别 sing-box JSON 官方订阅 (sbox.json / config.json)
    2. 自动识别标准 Base64 节点池 (vless/trojan/ss)

    errors 回答“为什么没认出来、丢了哪些”。这个错误通道此前是缺的——取订阅失败、
    返回 403 页面、内容不认，全都表现成“0 个节点而且一句话都没有”，与“没配节点”
    无从分辨。
    """
    s = raw_text.strip()
    if not s:
        return [], ["订阅内容为空"]
    # 1. 尝试作为 JSON 解析 (sing-box 订阅)
    if s.startswith("{") and s.endswith("}"):
        try:
            data = json.loads(s)
        except ValueError as exc:
            return [], [f"订阅内容像 JSON 但解析失败: {exc}"]
        if not isinstance(data, dict):
            return [], ["订阅 JSON 顶层不是对象"]
        outbounds = data.get("outbounds")
        if not isinstance(outbounds, list) or not outbounds:
            return [], [
                "订阅 JSON 里没有 outbounds 数组"
                "（裸单出站 / naiveproxy 形态请改用「导入 JSON」）"
            ]
        # 与「导入 JSON」走同一份校验与 tag 归一化：此前这里只按类型粗筛，
        # 缺凭据/缺 tls/撞内置 tag 的节点都会被原样收下。
        nodes, errors, _renamed = _validate_outbounds(outbounds, set(_BUILTIN_TAGS))
        if nodes:
            return nodes, errors
        return [], errors or [f"订阅 JSON 里 {len(outbounds)} 个出站没有一个是可用节点"]

    # 2. 尝试作为 Base64 订阅解析
    cleaned = s.replace(" ", "").replace("\n", "").replace("\r", "")
    missing_padding = len(cleaned) % 4
    if missing_padding:
        cleaned += "=" * (4 - missing_padding)
    try:
        decoded = base64.b64decode(cleaned).decode("utf-8", errors="ignore")
    except Exception as exc:
        return [], [f"订阅既不是 sing-box JSON、也无法按 Base64 解码: {exc}"]
    lines = [line.strip() for line in decoded.splitlines() if line.strip()]
    nodes: list[dict[str, Any]] = []
    for line in lines:
        parsed = parse_node_uri(line)
        if parsed:
            nodes.append(parsed)
    if not nodes:
        return [], [f"订阅解出 {len(lines)} 行，但没有一行是可识别的节点链接"]
    skipped = len(lines) - len(nodes)
    return nodes, (
        [f"另有 {skipped} 行不是可识别的节点链接，已跳过"] if skipped else []
    )


# sing-box 原生 JSON 导入支持的出站类型（用随包内核 sing-box check 实测）。
# wireguard 出站自 1.11.0 起弃用、1.13.0 起移除，故不在此列。
_IMPORT_SUPPORTED_TYPES = {
    "vless",
    "vmess",
    "trojan",
    "shadowsocks",
    "hysteria",
    "hysteria2",
    "tuic",
    "anytls",
    "shadowtls",
    "ssh",
    "naive",
}

# 各协议必需的凭据字段（缺一条即跳过并说明缺什么）。
# sing-box check/run 对这些字段多数不报错（“内核接受”不等于“能跑”），
# 因此导入端必须自己校验，否则坏节点会拖到测速/切换通道时才暴露。
_IMPORT_REQUIRED_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "vless": ("uuid",),
    "vmess": ("uuid",),
    "trojan": ("password",),
    "shadowsocks": ("method", "password"),
    "hysteria": ("auth_str",),
    "hysteria2": ("password",),
    "tuic": ("uuid", "password"),
    "anytls": ("password",),
    "shadowtls": ("password",),
    "ssh": ("user",),
    "naive": ("username", "password"),
}

# 这些协议的出站必须带 tls 结构（内核 check 报 “TLS required”），
# 与凭据一样属于“导入端必须自校验”的必需结构，缺了到用时才失败。
_IMPORT_REQUIRED_TLS = {"hysteria", "hysteria2", "tuic", "anytls", "shadowtls", "naive"}

# build_minimal_config 里写死的内置出站 tag，导入项撞名会让内核在启动时才报错
_BUILTIN_TAGS = {"direct", "block"}

# socks/http 只能指向远端出口；指向本地就是被移除的 BYO，导入时明确拒绝
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def naiveproxy_to_outbound(
    data: dict[str, Any], tag: str = ""
) -> dict[str, Any] | None:
    """把 naiveproxy 客户端配置转成 sing-box naive 出站。

    形态：{"listen": "socks://127.0.0.1:1080", "proxy": "https://user:pass@host:port"}
    naive 走 TLS，故只接受 https 形态的 proxy；无法识别返回 None。
    """
    raw = data.get("proxy")
    if not isinstance(raw, str) or not raw.strip():
        return None
    parsed = urlsplit(raw.strip())
    if parsed.scheme != "https" or not parsed.hostname or not parsed.port:
        return None
    return {
        "type": "naive",
        "tag": tag or f"naive-{parsed.hostname}:{parsed.port}",
        "server": parsed.hostname,
        "server_port": parsed.port,
        "username": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "tls": {"enabled": True, "server_name": parsed.hostname},
    }


def _validate_outbounds(
    outbounds: list[Any], seen_tags: set[str]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """逐条校验 + tag 归一化；两条入口（导入文件 / 订阅返回 JSON）**共用这一份**。

    返回 (nodes, errors, renamed)：
    - 类型必须在 `_IMPORT_SUPPORTED_TYPES` 里（`socks`/`http` 一律不收，指向本地时
      额外说明原因）；
    - `server` / `server_port` 必填；
    - 按协议校验必需凭据（`_IMPORT_REQUIRED_CREDENTIALS`）与必需 `tls`；
    - tag 撞内置出站（`direct`/`block`）或撞已见 tag → 改名 `imported-N`——不改名
      会让内核在启动时报 duplicate tag 直接 FATAL，而用户只看到下游一句“所有候选
      节点均无法建立有效转发通道”。
    """
    nodes: list[dict[str, Any]] = []
    errors: list[str] = []
    renamed: list[str] = []
    for idx, ob in enumerate(outbounds):
        if not isinstance(ob, dict):
            errors.append(f"第 {idx + 1} 项不是对象，已跳过")
            continue
        ob_type = ob.get("type")
        if ob_type in ("socks", "http"):
            if str(ob.get("server") or "") in _LOCAL_HOSTS:
                errors.append(
                    f"第 {idx + 1} 项 ({ob_type}) 指向本地出口，已跳过"
                    f"（本地代理请直接使用，不要经本工具）"
                )
            else:
                errors.append(f"第 {idx + 1} 项类型不受支持: {ob_type}")
            continue
        if ob_type not in _IMPORT_SUPPORTED_TYPES:
            errors.append(f"第 {idx + 1} 项类型不受支持: {ob_type}")
            continue
        if not ob.get("server"):
            errors.append(f"第 {idx + 1} 项 ({ob_type}) 缺 server，已跳过")
            continue
        if not ob.get("server_port"):
            errors.append(f"第 {idx + 1} 项 ({ob_type}) 缺 server_port，已跳过")
            continue
        if ob_type in _IMPORT_REQUIRED_TLS and not ob.get("tls"):
            errors.append(f"第 {idx + 1} 项 ({ob_type}) 缺 tls，已跳过")
            continue

        missing = [
            field
            for field in _IMPORT_REQUIRED_CREDENTIALS.get(ob_type, ())
            if not ob.get(field)
        ]
        # ssh 的认证要么密码要么私钥，二者至少一个
        if ob_type == "ssh" and not (ob.get("password") or ob.get("private_key")):
            missing.append("password 或 private_key")
        if missing:
            errors.append(
                f"第 {idx + 1} 项 ({ob_type}) 缺字段: {', '.join(missing)}，已跳过"
            )
            continue

        tag = ob.get("tag")
        if tag and tag in seen_tags:
            n = 1
            while f"imported-{n}" in seen_tags:
                n += 1
            new_tag = f"imported-{n}"
            renamed.append(f"tag '{tag}' 撞名，已改名 '{new_tag}'")
            ob = dict(ob)
            ob["tag"] = new_tag
            tag = new_tag
        if tag:
            seen_tags.add(tag)
        nodes.append(ob)

    return nodes, errors, renamed


def parse_singbox_json(
    text: str,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """解析 sing-box 原生 JSON 订阅文本为节点列表。

    只接受 `{"outbounds": [...]}` 形态，节点字段（tls / transport / multiplex 等
    嵌套结构）原样透传，不挑字段重建。返回三元组 (nodes, errors, renamed)：

    - nodes：通过校验、可直接塞进 outbounds 的节点（tag 撞名时已改名）；
    - errors：逐条跳过原因（非法 JSON / 非数组 / 类型不支持 / 缺字段）；
    - renamed：tag 撞名改名的说明，供面板提示“已改名”。
    """
    nodes: list[dict[str, Any]] = []
    renamed: list[str] = []

    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        return nodes, [f"JSON 解析失败: {exc}"], renamed

    if not isinstance(data, dict):
        return nodes, ["顶层不是 JSON 对象"], renamed

    outbounds = data.get("outbounds")
    if not isinstance(outbounds, list):
        # 兼容裸单出站：{"type": "naive", ...} 视作单元素 outbounds
        if isinstance(data.get("type"), str):
            outbounds = [data]
        else:
            # 兼容 naiveproxy 客户端配置：{"listen": ..., "proxy": "https://..."}
            converted = naiveproxy_to_outbound(data)
            if converted is None:
                return nodes, ["缺少 outbounds 数组"], renamed
            outbounds = [converted]
    if not outbounds:
        return nodes, ["outbounds 为空"], renamed

    seen_tags = set(_BUILTIN_TAGS)
    return _validate_outbounds(outbounds, seen_tags)


def parse_single_node(text: str) -> dict[str, Any] | None:
    """单节点输入：以 { 开头走 JSON（取 outbounds/endpoints 第一条可用项），否则走 URI。

    JSON 分支复用 parse_singbox_json（不另写一套）；endpoints 原样返回（运行期
    按 endpoints 而非 outbounds 走）。
    """
    s = text.strip()
    if not s.startswith("{"):
        return parse_node_uri(s)
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    outbounds = data.get("outbounds")
    if isinstance(outbounds, list) and outbounds:
        nodes, _errors, _renamed = parse_singbox_json(
            json.dumps({"outbounds": outbounds})
        )
        return nodes[0] if nodes else None
    endpoints = data.get("endpoints")
    if isinstance(endpoints, list) and endpoints and isinstance(endpoints[0], dict):
        return endpoints[0]
    if isinstance(data.get("type"), str):
        # 裸单出站：{"type": "naive", ...}
        nodes, _errors, _renamed = parse_singbox_json(json.dumps({"outbounds": [data]}))
        return nodes[0] if nodes else None
    # naiveproxy 客户端配置：{"listen": ..., "proxy": "https://..."}
    converted = naiveproxy_to_outbound(data)
    if converted is not None:
        nodes, _errors, _renamed = parse_singbox_json(
            json.dumps({"outbounds": [converted]})
        )
        return nodes[0] if nodes else None
    return None
