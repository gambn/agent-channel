"""应用图标资产的门禁：图必须由脚本产出，且三平台各按各的口径。

存在的理由：`packaging/assets/` 里的 PNG/ICO 全是 `packaging/make_icon.py` 的产物，
但此前没有任何机器校验。有人手改一张图而忘了改脚本，二进制 diff 看不出来，肉眼也难发现
（与内核哈希门禁是同一个思路）。

为什么按**像素**比而不是按字节比：PNG 的 IDAT 压缩流随 Pillow/zlib 版本变化。实测仓库里的
`tray.png` 与当前 Pillow 重跑出的字节不同（3438 vs 3563 字节），但解码后逐像素一致——
按字节比会在换构建机时假红。像素一致即"没人手改图不改脚本"。

Pillow 是**构建期**依赖（运行时零依赖），本机没装就 skip 并说明，与内核类测试同一风格。
"""

import importlib.util
import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "packaging" / "assets"
WEB = ROOT / "agent_channel" / "web"

PNG_ASSETS = ("icon.png", "icon-512.png", "icon-1024.png", "tray.png")
ICO_ASSETS = ("icon.ico", "tray.ico")


def _load_make_icon() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ac_make_icon", ROOT / "packaging" / "make_icon.py"
    )
    assert spec is not None, "无法为 packaging/make_icon.py 构造模块 spec"
    assert spec.loader is not None, "该 spec 没有 loader"
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["ac_make_icon"] = module
    spec.loader.exec_module(module)
    return module


def _regenerate(tmp_dir: Path) -> tuple[Path, Path]:
    """在临时目录里重跑一遍 make_icon（正式方案 + 托盘标），返回 (assets, web)。"""
    module = _load_make_icon()
    assets = tmp_dir / "assets"
    web = tmp_dir / "web"
    assets.mkdir()
    web.mkdir()
    module.ASSETS = assets
    module.WEB = web
    saved_argv = sys.argv
    try:
        for argv in (["make_icon.py"], ["make_icon.py", "--tray"]):
            sys.argv = argv
            with redirect_stdout(io.StringIO()):
                assert module.main() == 0, f"{argv} 返回非 0"
    finally:
        sys.argv = saved_argv
    return assets, web


def _rgba_pixels(path: Path) -> tuple[tuple[int, int], bytes]:
    """把图解码成 RGBA 像素字节（PIL 的类型存根不精确，这里统一按 Any 用）。"""
    from PIL import Image

    image: Any = Image.open(path)
    with image:
        converted: Any = image.convert("RGBA")
        return tuple(converted.size), converted.tobytes()


def _ico_frames(path: Path) -> dict[tuple[int, int], bytes]:
    """ICO 每档尺寸各自的 RGBA 像素字节。"""
    from PIL import Image

    image: Any = Image.open(path)
    frames: dict[tuple[int, int], bytes] = {}
    with image:
        for size in sorted(image.ico.sizes()):
            image.size = size
            frames[tuple(size)] = image.convert("RGBA").tobytes()
    return frames


def _alpha_bbox(path: Path) -> tuple[int, int, int, int] | None:
    from PIL import Image

    image: Any = Image.open(path)
    with image:
        return image.convert("RGBA").getchannel("A").getbbox()


def _corner_alphas(path: Path) -> list[int]:
    from PIL import Image

    image: Any = Image.open(path)
    with image:
        rgba: Any = image.convert("RGBA")
        width, height = rgba.size
        return [
            int(rgba.getpixel(point)[3])
            for point in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
        ]


def test_icon_assets_match_the_script_that_generates_them():
    """仓库里的图标资产 == 现脚本重跑出的像素（含 logo.svg 的逐字节一致）。"""
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("    [skip] 本机无 Pillow（构建期依赖），跳过图标复现性校验")
        return

    with tempfile.TemporaryDirectory() as tmp:
        fresh, fresh_web = _regenerate(Path(tmp))

        for name in PNG_ASSETS:
            committed_size, committed_pixels = _rgba_pixels(ASSETS / name)
            rebuilt_size, rebuilt_pixels = _rgba_pixels(fresh / name)
            assert committed_size == rebuilt_size, f"{name} 尺寸与脚本产出一致性被破坏"
            assert committed_pixels == rebuilt_pixels, (
                f"{name} 的像素与 make_icon.py 重跑结果不一致（手改过图却没改脚本？）"
            )

        for name in ICO_ASSETS:
            committed_frames = _ico_frames(ASSETS / name)
            rebuilt_frames = _ico_frames(fresh / name)
            assert sorted(committed_frames) == sorted(rebuilt_frames), (
                f"{name} 的尺寸档位与脚本产出不一致"
            )
            for size, pixels in committed_frames.items():
                assert pixels == rebuilt_frames[size], (
                    f"{name} 的 {size[0]}x{size[1]} 档像素与脚本产出不一致"
                )

        committed_favicon = _ico_frames(WEB / "favicon.ico")
        rebuilt_favicon = _ico_frames(fresh_web / "favicon.ico")
        assert sorted(committed_favicon) == sorted(rebuilt_favicon), (
            "favicon.ico 的尺寸档位与脚本产出不一致"
        )
        for size, pixels in committed_favicon.items():
            assert pixels == rebuilt_favicon[size], (
                f"favicon.ico 的 {size[0]}x{size[1]} 档像素与脚本产出不一致"
            )

        # 面板的两份矢量标（浏览器标签页的 favicon 与标题里的品牌标）都由脚本产出
        for name in ("logo.svg", "favicon.svg"):
            assert (WEB / name).read_bytes() == (fresh_web / name).read_bytes(), (
                f"{name} 与 make_icon.py 产出的矢量标不一致"
            )


def test_icon_geometry_follows_each_platform_convention():
    """口径本身就是承诺：macOS 居中留白 + 圆角、Linux 满幅 + 圆角、Windows 满幅方角。"""
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("    [skip] 本机无 Pillow（构建期依赖），跳过图标几何校验")
        return

    mac_box = _alpha_bbox(ASSETS / "icon-1024.png")
    assert mac_box is not None, "macOS 图标整张透明？"
    left, top, right, bottom = mac_box
    assert 80 <= left <= 120 and 80 <= top <= 120, f"macOS 图标应四周留白约 100px，实测 {mac_box}"
    assert 1024 - right <= 120 and 1024 - bottom <= 120, f"macOS 图标右下留白不足：{mac_box}"
    assert _corner_alphas(ASSETS / "icon-1024.png") == [0, 0, 0, 0], (
        "macOS 图标四角应透明（Apple 几何是圆角超椭圆）"
    )

    assert _alpha_bbox(ASSETS / "icon-512.png") == (0, 0, 512, 512), (
        "Linux 图标应满幅（四周不留白）"
    )
    assert _corner_alphas(ASSETS / "icon-512.png") == [0, 0, 0, 0], "Linux 图标四角应切圆角"

    windows_frames = _ico_frames(ASSETS / "icon.ico")
    largest = max(windows_frames)
    assert largest == (256, 256), f"Windows 图标最大档应是 256x256，实测 {largest}"
    assert _corner_alphas(ASSETS / "icon.ico") == [255, 255, 255, 255], (
        "Windows 图标应满幅方角（四角不透明）"
    )
