"""生成应用图标。仅构建期使用（依赖 Pillow），产物是静态 .ico/.png 资源。

用法：
    python packaging/make_icon.py                 # 生成正式图标（默认 double_arrow 方案）
    python packaging/make_icon.py --variant X      # 指定方案
    python packaging/make_icon.py --sheet          # 生成方案对比图，便于挑选
"""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent / "assets"
WEB = Path(__file__).resolve().parent.parent / "agent_channel" / "web"
SIZE = 256
RADIUS = 56

INDIGO = (79, 70, 229)
INDIGO_LIGHT = (129, 140, 248)
CYAN = (6, 182, 212)
CYAN_LIGHT = (34, 211, 238)
WHITE = (255, 255, 255, 255)

# 双向箭头（正式方案）配色，取自设计稿
ARROW_BG = (236, 154, 19)
ARROW_INK = (20, 20, 20, 255)

# 图标各档尺寸：Windows 在不同场景取不同尺寸（托盘 16 / 任务栏 32 / 资源管理器 48、256）
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

SS = 4  # 超采样倍数


def _render(size: int, background: tuple[int, ...], paint) -> Image.Image:
    """在 size×SS 的画布上作画再降采样，得到抗锯齿的边缘。

    Pillow 的绘图默认不做抗锯齿，直接按目标尺寸画会在斜边上留下台阶；
    先放大作画、再用 LANCZOS 缩回，相当于 4× 超采样。
    """
    px = size * SS
    canvas = Image.new("RGBA", (px, px), background)
    paint(ImageDraw.Draw(canvas), px)
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def _s(value: int, size: int) -> int:
    """把基于 256 基准的坐标等比缩放到目标尺寸（纯整数运算）。"""
    return value * size // SIZE


def _background(size: int = SIZE, radius: int = RADIUS) -> Image.Image:
    """圆角方形 + 对角渐变底色。"""
    grad = Image.new("RGB", (2, 2))
    grad.putpixel((0, 0), INDIGO)
    grad.putpixel((1, 0), INDIGO_LIGHT)
    grad.putpixel((0, 1), CYAN)
    grad.putpixel((1, 1), CYAN_LIGHT)
    grad = grad.resize((size, size), Image.Resampling.BICUBIC).convert("RGBA")

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=max(4, _s(radius, size)), fill=255
    )
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(grad, (0, 0), mask)
    return canvas


def _arrow(
    draw: ImageDraw.ImageDraw,
    size: int,
    x0: int,
    x1: int,
    y: int,
    shaft: int,
    head: int,
) -> None:
    """向右的箭头：粗杆 + 实心箭头，坐标同样按 256 基准缩放。"""
    sx0, sx1, sy = _s(x0, size), _s(x1, size), _s(y, size)
    shaft_w, head_w = _s(shaft, size), _s(head, size)
    half = shaft_w // 2
    draw.rectangle((sx0, sy - half, sx1 - head_w // 2, sy + half), fill=WHITE)
    draw.polygon(
        [
            (sx1 - head_w, sy - half - shaft_w // 4),
            (sx1, sy),
            (sx1 - head_w, sy + half + shaft_w // 4),
        ],
        fill=WHITE,
    )


def variant_arch_solid(size: int = SIZE) -> Image.Image:
    """方案四：实心拱门（通道），无内嵌细节，小尺寸最稳。"""
    img = _background(size)
    draw = ImageDraw.Draw(img)
    draw.pieslice(
        (_s(66, size), _s(48, size), _s(190, size), _s(172, size)),
        start=180,
        end=360,
        fill=WHITE,
    )
    draw.rectangle(
        (_s(66, size), _s(110, size), _s(190, size), _s(206, size)), fill=WHITE
    )
    return img


def variant_tunnel_arrow(size: int = SIZE) -> Image.Image:
    """方案一：隧道拱门 + 穿行的箭头（正式方案）。

    小尺寸（<=20px，典型托盘尺寸）下箭头会糊成一团，按图标设计惯例改用
    简化形态：只留实心拱门，保证在托盘里依然辨识得出来。
    """
    if size <= 20:
        return variant_arch_solid(size)

    img = _background(size)
    draw = ImageDraw.Draw(img)
    width = _s(32, size)
    draw.arc(
        (_s(60, size), _s(50, size), _s(196, size), _s(186, size)),
        start=180,
        end=360,
        fill=WHITE,
        width=width,
    )
    for x in (_s(60, size) + width // 2, _s(196, size) - width // 2):
        draw.line((x, _s(118, size), x, _s(206, size)), fill=WHITE, width=width)
    _arrow(draw, size, 92, 166, 152, 22, 42)
    return img


def variant_arrow_only(size: int = SIZE) -> Image.Image:
    """方案二：纯箭头，最简、小尺寸最清晰。"""
    img = _background(size)
    _arrow(ImageDraw.Draw(img), size, 56, 206, 128, 34, 66)
    return img


def variant_portal(size: int = SIZE) -> Image.Image:
    """方案三：实心拱门（通道）+ 横穿的箭头。"""
    img = _background(size)
    draw = ImageDraw.Draw(img)
    transparent = (0, 0, 0, 0)
    draw.pieslice(
        (_s(70, size), _s(52, size), _s(186, size), _s(168, size)),
        start=180,
        end=360,
        fill=WHITE,
    )
    draw.rectangle(
        (_s(70, size), _s(110, size), _s(186, size), _s(196, size)), fill=WHITE
    )
    draw.pieslice(
        (_s(96, size), _s(78, size), _s(160, size), _s(142, size)),
        start=180,
        end=360,
        fill=transparent,
    )
    draw.rectangle(
        (_s(96, size), _s(110, size), _s(160, size), _s(196, size)), fill=transparent
    )
    _arrow(draw, size, 90, 170, 150, 18, 34)
    return img


# 双向箭头（正式方案）几何，256 基准；位图与矢量（web/logo.svg）共用一套
ARROW_TOP, ARROW_BOTTOM = 55, 200
ARROW_HEAD_H = 55
ARROW_SHAFT_HW, ARROW_HEAD_HW = 12, 29
ARROW_LX, ARROW_RX = 99, 157


def _paint_arrows(draw: ImageDraw.ImageDraw, px: int, ink: tuple[int, ...]) -> None:
    """按 256 基准画两支箭头（左向下、右向上）。位图各方案共用这一套几何。"""
    top, bottom = _s(ARROW_TOP, px), _s(ARROW_BOTTOM, px)
    head_h = _s(ARROW_HEAD_H, px)
    shaft_hw, head_hw = _s(ARROW_SHAFT_HW, px), _s(ARROW_HEAD_HW, px)
    lx, rx = _s(ARROW_LX, px), _s(ARROW_RX, px)
    # 左：向下
    draw.rectangle((lx - shaft_hw, top, lx + shaft_hw, bottom - head_h), fill=ink)
    draw.polygon(
        [
            (lx - head_hw, bottom - head_h),
            (lx + head_hw, bottom - head_h),
            (lx, bottom),
        ],
        fill=ink,
    )
    # 右：向上
    draw.polygon(
        [(rx - head_hw, top + head_h), (rx + head_hw, top + head_h), (rx, top)],
        fill=ink,
    )
    draw.rectangle((rx - shaft_hw, top + head_h, rx + shaft_hw, bottom), fill=ink)


def variant_double_arrow(size: int = SIZE) -> Image.Image:
    """方案五：双向箭头（橙底 + 左向下 / 右向上两支箭头），满幅底色、无圆角。

    通道的“上下行”意象：一支下行、一支上行并排，箭头相对、中间留一道细缝。
    """
    return _render(
        size, ARROW_BG + (255,), lambda draw, px: _paint_arrows(draw, px, ARROW_INK)
    )


def variant_tray_arrow(size: int = 32) -> Image.Image:
    """托盘方案：与正式标同形的双向箭头，去掉橙底色块（透明底）。

    形状与 double_arrow 完全一致（同一套几何、同一个颜色），只是不画底色块，
    这样托盘与面板小图标和品牌标是同一个图案，不做二次设计。
    """
    return _render(
        size, (0, 0, 0, 0), lambda draw, px: _paint_arrows(draw, px, ARROW_INK)
    )


VARIANTS = {
    "double_arrow": variant_double_arrow,
    "tunnel_arrow": variant_tunnel_arrow,
    "arrow_only": variant_arrow_only,
    "arch_solid": variant_arch_solid,
    "portal": variant_portal,
    "tray_arrow": variant_tray_arrow,
}


def trim_source(image: Image.Image, threshold: int = 200) -> Image.Image:
    """裁到不透明主体的外接框，并补成正方形。

    大尺寸源图常在底部带一层倒影（alpha 明显低于主体，不属于图标本体）。
    用较高的 alpha 阈值取外接框即可把它排除，同时保留主体自己的柔和边缘；
    最后补成正方形，避免各尺寸渲染时被拉伸变形。
    """
    rgba = image.convert("RGBA")
    lookup = [0 if value < threshold else 255 for value in range(256)]
    mask = rgba.getchannel("A").point(lookup)
    bbox = mask.getbbox()
    if bbox is None:
        return rgba
    cropped = rgba.crop(bbox)
    side = max(cropped.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(
        cropped,
        ((side - cropped.width) // 2, (side - cropped.height) // 2),
        cropped,
    )
    return canvas


def write_ico(image: Image.Image, path: Path) -> None:
    """写入多尺寸 ico。

    Windows 会在不同场景取不同尺寸（托盘 16、任务栏 32、资源管理器 48/256），
    单尺寸图标会被系统拉伸导致模糊，所以统一生成这七档。
    """
    sizes = [(side, side) for side in ICO_SIZES]
    image.save(path, format="ICO", sizes=sizes)


def load_source(source: Path) -> Image.Image:
    """载入外部图标为 RGBA（保持原始像素尺寸，不预先放大）。"""
    return Image.open(source).convert("RGBA")


def write_logo_svg(path: Path) -> None:
    """导出正式品牌标的矢量版（橙底 + 双向箭头），供面板标题使用。

    几何取自 double_arrow 的常量，与位图永远一致。
    """
    top, bottom = ARROW_TOP, ARROW_BOTTOM
    head_h = ARROW_HEAD_H
    shaft_hw, head_hw = ARROW_SHAFT_HW, ARROW_HEAD_HW
    lx, rx = ARROW_LX, ARROW_RX
    bg = "#" + "".join(f"{c:02x}" for c in ARROW_BG)
    ink = "#" + "".join(f"{c:02x}" for c in ARROW_INK[:3])

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" '
        'width="256" height="256" role="img" aria-label="agent-channel">\n'
        f'  <rect width="256" height="256" fill="{bg}"/>\n'
        f'  <rect x="{lx - shaft_hw}" y="{top}" width="{shaft_hw * 2}" '
        f'height="{bottom - head_h - top}" fill="{ink}"/>\n'
        f'  <polygon points="{lx - head_hw},{bottom - head_h} '
        f'{lx + head_hw},{bottom - head_h} {lx},{bottom}" fill="{ink}"/>\n'
        f'  <polygon points="{rx - head_hw},{top + head_h} '
        f'{rx + head_hw},{top + head_h} {rx},{top}" fill="{ink}"/>\n'
        f'  <rect x="{rx - shaft_hw}" y="{top + head_h}" width="{shaft_hw * 2}" '
        f'height="{bottom - head_h - top}" fill="{ink}"/>\n'
        "</svg>\n"
    )
    # 显式 newline="\n"：Windows 的 text mode 默认写成 CRLF，会与本仓库
    # .gitattributes 的 eol=lf 冲突（每次重跑生成器都会把工作区改成 CRLF）。
    path.write_text(svg, encoding="utf-8", newline="\n")


def _bgra_bottom_up(frame: Image.Image) -> bytes:
    """DIB 像素数据：自下而上的 BGRA。"""
    width, height = frame.size
    raw = bytearray(frame.convert("RGBA").tobytes())
    # RGBA -> BGRA（先取右侧切片再赋值，避免边写边读）
    raw[0::4], raw[2::4] = raw[2::4], raw[0::4]
    row = width * 4
    flipped = bytearray()
    for y in range(height - 1, -1, -1):
        flipped += raw[y * row : (y + 1) * row]
    return bytes(flipped)


def write_ico_frames(frames: list[Image.Image], path: Path) -> None:
    """把逐档单独渲染的图写成多尺寸 ico。

    Pillow 的 ICO 保存只接受单一基准图并自行缩放，会把为小尺寸专门画的描边
    平滑掉；而托盘图标正是靠这条描边在小尺寸下保持辨识度，所以自行组装容器。
    帧用 BMP/DIB（最通用的格式），不用 PNG 帧以避开旧组件的兼容问题。
    """
    import struct

    entries = b""
    blobs = b""
    offset = 6 + 16 * len(frames)
    for frame in frames:
        width, height = frame.size
        pixels = _bgra_bottom_up(frame.convert("RGBA"))
        header = struct.pack(
            "<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0
        )
        mask_row = ((width + 31) // 32) * 4
        data = header + pixels + b"\x00" * (mask_row * height)
        entries += struct.pack(
            "<BBBBHHII", width % 256, height % 256, 0, 0, 1, 32, len(data), offset
        )
        blobs += data
        offset += len(data)

    path.write_bytes(struct.pack("<HHH", 0, 1, len(frames)) + entries + blobs)


def build_sheet() -> Path:
    """把各方案渲染成对比图：256px 原图 + 32px/16px 放大预览。"""
    cell_w, cell_h = 300, 380
    sheet = Image.new("RGBA", (cell_w * len(VARIANTS), cell_h), (245, 246, 248, 255))
    draw = ImageDraw.Draw(sheet)
    for index, (name, factory) in enumerate(VARIANTS.items()):
        x = index * cell_w
        big = factory(SIZE)
        preview_32 = factory(32).resize((64, 64), Image.Resampling.NEAREST)
        preview_16 = factory(16).resize((64, 64), Image.Resampling.NEAREST)
        sheet.paste(big, (x + 22, 40), big)
        sheet.paste(preview_32, (x + 22, 296), preview_32)
        sheet.paste(preview_16, (x + 110, 296), preview_16)
        draw.text((x + 22, 18), name, fill=(20, 20, 20, 255))
        draw.text((x + 22, 364), "left:32px  right:16px", fill=(90, 90, 90, 255))
    out = ASSETS / "icon_variants.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="生成应用图标资源")
    parser.add_argument("--variant", choices=sorted(VARIANTS), default="double_arrow")
    parser.add_argument("--sheet", action="store_true", help="只生成方案对比图")
    parser.add_argument(
        "--tray",
        action="store_true",
        help="生成托盘专用图标（透明底、小尺寸单独绘制）",
    )
    parser.add_argument(
        "--from",
        dest="source",
        help="以外部图片/ico 为源生成多尺寸图标（优先于 --variant）",
    )
    parser.add_argument(
        "--trim",
        action="store_true",
        help="裁掉透明边距与倒影并补成正方形（适用于带倒影的大尺寸源图）",
    )
    args = parser.parse_args()

    ASSETS.mkdir(parents=True, exist_ok=True)

    if args.tray:
        # 逐档单独渲染，而不是从大图缩下来，小尺寸才锐利
        sizes = [16, 20, 24, 32, 48]
        frames = [variant_tray_arrow(size) for size in sizes]
        ico = ASSETS / "tray.ico"
        write_ico_frames(frames, ico)
        variant_tray_arrow(SIZE).save(ASSETS / "tray.png")
        print(f"已生成托盘图标: {ico} (逐档渲染 {sizes})")
        return 0
    if args.sheet:
        print("对比图:", build_sheet())
        return 0

    if args.source:
        source = Path(args.source)
        if not source.is_file():
            print(f"源图不存在: {source}")
            return 1
        native = load_source(source)
        if args.trim:
            native = trim_source(native)
        origin = f"外部源 {source.name}"
        if args.trim:
            origin += f" (裁切后 {native.width}x{native.height})"
    else:
        native = VARIANTS[args.variant]()
        origin = f"方案 {args.variant}"

    # PNG 预览统一到 256，便于查看
    preview = (
        native
        if native.size == (SIZE, SIZE)
        else native.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    )
    preview.save(ASSETS / "icon.png")
    # 高分辨率 PNG：README 用 512（GitHub 内容区约 820px，2x 屏需 ≥512），
    # 另出一档 1024 备用。走 variant 时按 256 基准的几何原生渲染，不是放大 256。
    for side in (512, 1024):
        high = (
            native.resize((side, side), Image.Resampling.LANCZOS)
            if args.source
            else VARIANTS[args.variant](side)
        )
        high.save(ASSETS / f"icon-{side}.png")
    if args.source:
        write_ico(native, ASSETS / "icon.ico")
    else:
        # 各尺寸逐档单独渲染：由大图缩下去会把小尺寸的笔形糊掉
        write_ico_frames(
            [VARIANTS[args.variant](side) for side in ICO_SIZES],
            ASSETS / "icon.ico",
        )
        if args.variant == "double_arrow":
            # 面板标题用的品牌标矢量版，与正式图标同源
            write_logo_svg(WEB / "logo.svg")
            print(f"已生成矢量品牌标: {WEB / 'logo.svg'}")
    print(f"已生成: {ASSETS / 'icon.png'} / {ASSETS / 'icon.ico'} ({origin})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
