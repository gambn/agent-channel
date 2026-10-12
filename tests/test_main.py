"""python -m agent_channel 模块入口的分发。"""

import sys
from unittest.mock import patch


def test_module_main_dispatches_to_cli_when_args_present():
    """python -m agent_channel <命令> 必须转 CLI，不得吞命令起面板。"""
    import agent_channel.__main__ as main_mod

    with (
        patch.object(main_mod, "launch_ui") as mock_ui,
        patch.object(sys, "argv", ["agent_channel", "status"]),
        patch("agent_channel.cli.main", return_value=0) as mock_cli,
    ):
        assert main_mod.main() == 0
        mock_cli.assert_called_once()
        mock_ui.assert_not_called()


def test_module_main_launches_panel_when_no_args():
    """python -m agent_channel（无参数）仍起面板。"""
    import agent_channel.__main__ as main_mod

    with (
        patch.object(main_mod, "launch_ui") as mock_ui,
        patch.object(sys, "argv", ["agent_channel"]),
    ):
        assert main_mod.main() == 0
        mock_ui.assert_called_once()
