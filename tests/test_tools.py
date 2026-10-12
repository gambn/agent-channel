from agent_channel.tools import (
    AGENT_TOOLS,
    execute_natural_language,
)


def test_agent_tools_schema():
    assert len(AGENT_TOOLS) == 5
    names = [t["function"]["name"] for t in AGENT_TOOLS]
    assert "channel_enable" in names
    assert "channel_disable" in names
    assert "channel_test_ai" in names


def test_natural_language_uuid():
    res = execute_natural_language("请帮我生成一个 UUID 给节点用")
    assert "已生成新 UUID" in res


def test_natural_language_disable():
    res = execute_natural_language("帮我把网络代理通道关掉")
    assert "已成功停止" in res


def test_natural_language_status():
    res = execute_natural_language("当前通道运行状态怎么样了")
    assert "停止状态" in res or "运行中" in res


def test_natural_language_local_port_rejected():
    """删 BYO 后：本地端口不再被识别为通道目标（不再连本地代理）。"""
    res = execute_natural_language("请帮我连接本地代理端口 7890")
    assert "未能识别" in res


def test_natural_language_submit_not_subscription():
    """URL 里含 submit 子串不应被误判为订阅指令（sub 需词边界匹配）。"""
    res = execute_natural_language("打开 https://example.com/submit")
    assert "未能识别" in res


def test_natural_language_unknown():
    res = execute_natural_language("今天天气怎么样")
    assert "未能识别该通道指令" in res


def test_cli_status_does_not_nameerror():
    """cli status 命令调用的 config_warning 必须已 import（曾因未 import 报 NameError）。"""
    import argparse
    from unittest.mock import patch

    from agent_channel import cli

    with patch.object(cli, "_remote_status", return_value=None):
        ret = cli._cmd_status(argparse.Namespace())
    assert ret == cli.EXIT_OK


def test_cli_set_rejects_empty_value():
    """set-node / set-sub 空值必须拒绝（断 B4 的 CLI 侧同根）。"""
    import argparse

    from agent_channel import cli

    assert cli._cmd_set(argparse.Namespace(mode="node", value="  ")) == cli.EXIT_ERROR
    assert cli._cmd_set(argparse.Namespace(mode="sub", value="")) == cli.EXIT_ERROR


def test_cli_set_json_imports_nodes_only():
    """set-json 从文件导入 sing-box JSON：只写 imported_nodes，不写 active_source。"""
    import argparse
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from agent_channel import cli
    from agent_channel import config as cfgmod

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        path = tmp_dir / "config.json"
        src_file = tmp_dir / "nodes.json"
        src_file.write_text(
            '{"outbounds": [{"type": "vless", "tag": "n", "server": "a.com", '
            '"server_port": 443, "uuid": "u"}]}',
            encoding="utf-8",
        )
        with patch.object(cfgmod, "config_path", lambda: path):
            ret = cli._cmd_set_json(argparse.Namespace(path=str(src_file)))
            assert ret == cli.EXIT_OK
            cfg = cfgmod.load_config()
            assert len(cfg["imported_nodes"]) == 1, cfg
            assert cfg["active_source"] == "", cfg  # 导入≠启用，不写 active_source


def test_cli_set_json_rejects_missing_file():
    """set-json 对不存在的文件必须拒绝 + 非零退出码。"""
    import argparse

    from agent_channel import cli

    assert (
        cli._cmd_set_json(argparse.Namespace(path="Z:/nope/nodes.json"))
        == cli.EXIT_ERROR
    )


def test_cli_config_masks_imported_credentials():
    """默认脱敏必须覆盖 imported_nodes 里的凭据；只有 --reveal 才给全量。

    红线：这条命令的输出会被 Agent 读进对话，节点凭据不得随"查看配置"外泄。导入节点
    的凭据字段随协议而变，所以脱敏按"只保留非敏感字段"做，而不是逐个枚举要遮的键。
    """
    import argparse
    import contextlib
    import io
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from agent_channel import cli
    from agent_channel import config as cfgmod

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        with patch.object(cfgmod, "config_path", lambda: path):
            assert cfgmod.save_config(
                {
                    "imported_nodes": [
                        {
                            "type": "vless",
                            "tag": "n",
                            "server": "a.com",
                            "server_port": 443,
                            "uuid": "SECRET-UUID",
                            "password": "SECRET-PW",
                        }
                    ]
                }
            )

            masked_out = io.StringIO()
            with contextlib.redirect_stdout(masked_out):
                assert cli._cmd_config(argparse.Namespace(reveal=False)) == cli.EXIT_OK
            masked = masked_out.getvalue()
            assert "SECRET-UUID" not in masked, masked
            assert "SECRET-PW" not in masked, masked
            assert "a.com" in masked, f"非敏感字段应保留，便于核对导入了什么: {masked}"

            revealed_out = io.StringIO()
            with contextlib.redirect_stdout(revealed_out):
                assert cli._cmd_config(argparse.Namespace(reveal=True)) == cli.EXIT_OK
            assert "SECRET-UUID" in revealed_out.getvalue(), "--reveal 应给全量"


def test_cli_stop_reports_daemon_busy_instead_of_claiming_stopped():
    """守护进程回 409（忙）时 stop 必须报错，不能打印 stopped。

    `_remote_post` 此前把 HTTP 错误和"不可达"一起吞成 None，于是 409 会被当成"守护进程
    不在"→ 回落到本进程 disable（本进程没有内核）→ 打印 stopped，而通道其实还在跑。
    """
    import argparse
    import contextlib
    import io
    from unittest.mock import patch

    from agent_channel import cli

    busy = {"error": "已有通道操作正在进行中，请等它结束后再试"}
    with patch.object(cli, "_remote_post", return_value=busy):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert cli._cmd_stop(argparse.Namespace()) == cli.EXIT_ERROR
    assert "进行中" in err.getvalue(), err.getvalue()

    with patch.object(cli, "_remote_post", return_value={"status": "stopped"}):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert cli._cmd_stop(argparse.Namespace()) == cli.EXIT_OK
    assert "stopped" in out.getvalue(), out.getvalue()


def test_remote_post_surfaces_server_error_body():
    """服务端明确答复的错误（如 409）要原样带回，不能和"不可达"一起变成 None。"""
    import email.message
    import io
    import json
    import urllib.error
    from unittest.mock import patch

    from agent_channel import cli

    body = json.dumps({"error": "已有通道操作正在进行中"}, ensure_ascii=False).encode()
    conflict = urllib.error.HTTPError(
        url="http://127.0.0.1:18888/api/disable",
        code=409,
        msg="Conflict",
        hdrs=email.message.Message(),  # 用真的空 Message，不用 None（类型上 hdrs 非可选）
        fp=io.BytesIO(body),
    )
    with patch("urllib.request.urlopen", side_effect=conflict):
        result = cli._remote_post("/api/disable", {})
    assert result == {"error": "已有通道操作正在进行中"}, result

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("拒绝连接")):
        assert cli._remote_post("/api/disable", {}) is None, "不可达仍应回 None"
