#!/usr/bin/env python3
"""
接收 UDP 5604 上的 RTP/MJPEG 视频并显示。

默认按常见 RTP/JPEG（payload=26）解析，必要时可通过命令行改 payload。
"""

from __future__ import annotations

import argparse
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

Gst.init(None)

PIPELINE_TEMPLATE = (
    'udpsrc port={port} buffer-size=2097152 '
    'caps="application/x-rtp,media=video,encoding-name=JPEG,payload={payload}" '
    "! rtpjpegdepay ! jpegdec ! videoconvert ! autovideosink sync=false"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="UDP RTP/MJPEG 视频查看器")
    parser.add_argument("--port", type=int, default=5604, help="UDP 端口（默认 5604）")
    parser.add_argument(
        "--payload",
        type=int,
        default=26,
        help="RTP payload type（默认 26，标准 JPEG）",
    )
    args = parser.parse_args()

    pipeline_str = PIPELINE_TEMPLATE.format(port=args.port, payload=args.payload)
    print(f"管道: {pipeline_str}")

    pipeline = Gst.parse_launch(pipeline_str)
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    pipeline.set_state(Gst.State.PLAYING)
    print(
        f"正在监听 UDP {args.port}（MJPG/RTP, payload={args.payload}），"
        "等待视频流… 关闭窗口或按 Ctrl+C 退出。"
    )

    loop = GLib.MainLoop()

    def on_message(_bus, msg):
        t = msg.type
        if t == Gst.MessageType.EOS:
            print("流结束 (EOS)。")
            loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            print(f"错误: {err.message}", file=sys.stderr)
            if debug:
                print(f"调试: {debug}", file=sys.stderr)
            loop.quit()

    bus.connect("message", on_message)

    try:
        loop.run()
    except KeyboardInterrupt:
        print("\n已中断。")
    finally:
        pipeline.set_state(Gst.State.NULL)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
