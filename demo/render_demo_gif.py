#!/usr/bin/env python3
"""Render a silent GIF from the honesty-gates demo typescript.

Reads /tmp/honesty-demo.typescript (captured with `script`), strips
terminal control sequences, then emulates an 80x24 terminal with
progressive line-by-line reveal, writing docs/assets/honesty-gates.gif.

The terminal session is real — this script only draws what it printed.
"""

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COLS, ROWS = 80, 24
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_SIZE = 14
BG = (18, 18, 22)
FG = (230, 230, 235)
FPS = 7
HOLD_LAST_SEC = 2.5

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][0-9A-B]|\x0f|\x07")


def strip_ansi(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    text = ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def wrap_line(line: str):
    """Hard-wrap at COLS like a real terminal (keep it simple: no word wrap)."""
    line = line.expandtabs(8)
    if not line:
        return [""]
    return [line[i:i + COLS] for i in range(0, len(line), COLS)] or [""]


def main():
    from PIL import Image, ImageDraw, ImageFont
    ts_path = os.environ.get("KEEL_DEMO_TYPESCRIPT", "/tmp/honesty-demo.typescript")
    out_path = os.path.join(REPO, "docs", "assets", "honesty-gates.gif")
    with open(ts_path, "rb") as f:
        text = strip_ansi(f.read())

    # Progressive screen states: one per raw line printed.
    wrapped = []
    for raw in text.split("\n"):
        if raw.startswith("Script done on"):
            continue  # `script` wrapper footer, not demo output
        wrapped.extend(wrap_line(raw))
    if wrapped and wrapped[-1] == "":
        wrapped.pop()

    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    bbox = font.getbbox("M")
    cw, ch = bbox[2] - bbox[0], (bbox[3] - bbox[1]) + 4
    W, H = COLS * cw, ROWS * ch

    frames = []
    seen = 0
    for i in range(1, len(wrapped) + 1):
        screen = wrapped[max(0, i - ROWS):i]
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        for r, line in enumerate(screen):
            d.text((0, r * ch), line, font=font, fill=FG)
        frames.append(img)
        seen = i

    if not frames:
        print("no output captured; aborting", file=sys.stderr)
        sys.exit(1)

    # Hold the final frame so the last screen state is readable.
    hold = int(FPS * HOLD_LAST_SEC)
    frames.extend([frames[-1]] * hold)

    # Scale 1.25x for legibility, then PNG-sequence them into /tmp and let
    # ffmpeg build an optimized GIF (palettegen/paletteuse) — far smaller
    # than PIL's full-frame GIF encoding for near-static text.
    import shutil
    import subprocess
    seqdir = "/tmp/honesty-demo-frames"
    shutil.rmtree(seqdir, ignore_errors=True)
    os.makedirs(seqdir)
    for i, f in enumerate(frames):
        f.resize((int(W * 1.25), int(H * 1.25)), Image.LANCZOS).save(
            os.path.join(seqdir, f"f{i:04d}.png"))
    pal = "/tmp/honesty-demo-palette.png"
    fps_str = str(FPS)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-framerate", fps_str,
                    "-i", os.path.join(seqdir, "f%04d.png"),
                    "-vf", "palettegen=max_colors=16", pal], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-framerate", fps_str,
                    "-i", os.path.join(seqdir, "f%04d.png"), "-i", pal,
                    "-lavfi", "paletteuse=dither=none",
                    out_path], check=True)
    size = os.path.getsize(out_path)
    print(f"wrote {out_path} ({len(frames)} frames, {size} bytes)")


if __name__ == "__main__":
    main()
