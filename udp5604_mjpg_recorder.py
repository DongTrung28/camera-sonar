#!/usr/bin/env python3
"""
接收 UDP 5604 上的 RTP/MJPEG 视频并保存到文件。

默认保存为 MKV（MJPEG 直存，协商更稳）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

Gst.init(None)

RECORD_ONLY_PIPELINE_TEMPLATE = (
    'udpsrc port={port} buffer-size=2097152 '
    'caps="application/x-rtp,media=video,encoding-name=JPEG,clock-rate=90000,payload={payload}" '
    "! rtpjpegdepay ! jpegparse ! {muxer} ! filesink location={output_path}"
)

PREVIEW_PIPELINE_TEMPLATE = (
    'udpsrc port={port} buffer-size=2097152 '
    'caps="application/x-rtp,media=video,encoding-name=JPEG,clock-rate=90000,payload={payload}" '
    "! rtpjpegdepay ! tee name=t "
    "t. ! queue ! jpegparse ! {muxer} ! filesink location={output_path} "
    "t. ! queue ! jpegdec ! videoconvert ! autovideosink sync=false"
)


def build_default_output() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(f"recordings/udp5604_{ts}.mkv")


def choose_muxer(output_path: Path) -> str:
    suffix = output_path.suffix.lower()
    if suffix == ".avi":
        return "avimux"
    return "matroskamux"


def build_pipeline(port: int, payload: int, muxer: str, output_path: str, preview: bool) -> str:
    template = PREVIEW_PIPELINE_TEMPLATE if preview else RECORD_ONLY_PIPELINE_TEMPLATE
    return template.format(
        port=port,
        payload=payload,
        muxer=muxer,
        output_path=output_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="UDP RTP/MJPEG 录像脚本")
    parser.add_argument("--port", type=int, default=5604, help="UDP 端口（默认 5604）")
    parser.add_argument(
        "--payload",
        type=int,
        default=26,
        help="RTP payload type（默认 26，标准 JPEG）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=build_default_output(),
        help="输出文件路径（默认 recordings/udp5604_时间戳.mkv；.avi 将使用 avimux）",
    )
    parser.add_argument(
        "--preview",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="录制同时显示预览窗口（默认关闭）",
    )
    args = parser.parse_args()

    output_path = args.output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    muxer = choose_muxer(output_path)

    # GStreamer 位置参数中路径需要安全引用。
    location_quoted = f'"{output_path.as_posix()}"'
    pipeline_str = build_pipeline(
        port=args.port,
        payload=args.payload,
        muxer=muxer,
        output_path=location_quoted,
        preview=args.preview,
    )
    print(f"管道: {pipeline_str}")

    pipeline = Gst.parse_launch(pipeline_str)
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    pipeline.set_state(Gst.State.PLAYING)
    print(
        f"正在监听 UDP {args.port}（MJPG/RTP, payload={args.payload}），"
        f"写入: {output_path}（{muxer}），预览: {'开' if args.preview else '关'}\n"
        "按 Ctrl+C 停止录制。"
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
        print("\n收到中断，正在停止并保存文件…")
    finally:
        pipeline.set_state(Gst.State.NULL)
        print(f"已结束录制: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
