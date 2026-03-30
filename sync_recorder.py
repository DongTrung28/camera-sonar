#!/usr/bin/env python3
"""
Synchronized recorder for BlueROV2 camera (UDP MJPEG on port 5604)
and WaterLinked Sonar 3D-15 (UDP multicast on 224.0.0.96:4747).

Both streams are recorded simultaneously with per-frame / per-packet
wall-clock timestamps saved to CSV files, enabling time-synchronized
playback with sync_visualizer.py.

Output (all in --output-dir, default: recordings/):
    camera_YYYYMMDD_HHMMSS.mkv              – camera video (MJPEG in MKV)
    sonar_YYYYMMDD_HHMMSS.sonar             – sonar recording (raw RIP2 packets)
    camera_YYYYMMDD_HHMMSS_timestamps.csv
    sonar_YYYYMMDD_HHMMSS_timestamps.csv

Usage:
    python sync_recorder.py [--camera-port 5604] [--sonar-port 4747]
                            [--sonar-multicast 224.0.0.96] [--output-dir recordings]
    Press 'q' in the preview window or Ctrl+C to stop and save.
"""

from __future__ import annotations

import argparse
import csv
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

# Add wlsonar to Python path (works whether installed or run from the ERA root)
_wlsonar_src = Path(__file__).parent / "wlsonar" / "src"
if _wlsonar_src.exists() and str(_wlsonar_src) not in sys.path:
    sys.path.insert(0, str(_wlsonar_src))

import wlsonar                              # noqa: E402
import wlsonar.range_image_protocol as rip  # noqa: E402


# ---------------------------------------------------------------------------
# Camera capture via GStreamer appsink
# ---------------------------------------------------------------------------

class CameraCapture:
    """Receives RTP/MJPEG frames from a UDP port and exposes them as BGR numpy arrays."""

    def __init__(self, port: int = 5604, payload: int = 26) -> None:
        self.port = port
        self.payload = payload
        self._pipeline = None
        self._appsink = None

    def start(self) -> None:
        Gst.init(None)
        pipeline_str = (
            f"udpsrc port={self.port} buffer-size=2097152 "
            f'caps="application/x-rtp,media=video,encoding-name=JPEG,'
            f'clock-rate=90000,payload={self.payload}" '
            "! rtpjpegdepay ! jpegparse "
            "! appsink name=appsink emit-signals=false sync=false drop=true max-buffers=1"
        )
        self._pipeline = Gst.parse_launch(pipeline_str)
        self._appsink = self._pipeline.get_by_name("appsink")
        self._pipeline.set_state(Gst.State.PLAYING)

    def read(self, timeout_ms: int = 100):
        """Pull one frame.  Returns (ok, bgr_frame, wall_clock_sec)."""
        sample = self._appsink.try_pull_sample(timeout_ms * 1_000_000)
        if sample is None:
            return False, None, None
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return False, None, None
        data = bytes(mapinfo.data)
        buf.unmap(mapinfo)
        arr = np.frombuffer(data, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return (frame is not None), frame, time.time()

    def stop(self) -> None:
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)


# ---------------------------------------------------------------------------
# Sonar capture
# ---------------------------------------------------------------------------

def _sonar_msg_to_display(msg) -> np.ndarray | None:
    """Convert a sonar protobuf message to an 8-bit greyscale numpy image."""
    try:
        if isinstance(msg, rip.BitmapImageGreyscale8):
            img = np.frombuffer(msg.image_pixel_data, dtype=np.uint8).reshape(
                msg.height, msg.width
            )
            return cv2.flip(img, 0)
        if isinstance(msg, rip.RangeImage):
            px = np.array(msg.image_pixel_data, dtype=np.float32)
            if px.max() > 0:
                px = (px / px.max() * 255).astype(np.uint8)
            else:
                px = px.astype(np.uint8)
            return cv2.flip(px.reshape(msg.height, msg.width), 0)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Main recording function
# ---------------------------------------------------------------------------

def record(args: argparse.Namespace) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    video_path   = out_dir / f"camera_{ts}.mkv"
    sonar_path   = out_dir / f"sonar_{ts}.sonar"
    cam_csv_path = out_dir / f"camera_{ts}_timestamps.csv"
    son_csv_path = out_dir / f"sonar_{ts}_timestamps.csv"

    stop_event = threading.Event()

    # Shared latest sonar display image (updated by sonar thread, read by main)
    latest_sonar: list[np.ndarray | None] = [None]
    sonar_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Camera setup
    # ------------------------------------------------------------------
    cam = CameraCapture(port=args.camera_port, payload=args.camera_payload)
    cam.start()

    print(f"Waiting for first camera frame on port {args.camera_port}...")
    first_frame = None
    for _ in range(100):
        ok, frame, _ = cam.read(timeout_ms=200)
        if ok and frame is not None:
            first_frame = frame
            break
    if first_frame is None:
        print(
            f"ERROR: No camera frame received on port {args.camera_port}. "
            "Check that the BlueROV2 is streaming MJPEG/RTP."
        )
        cam.stop()
        return

    h, w = first_frame.shape[:2]
    writer = cv2.VideoWriter(
        str(video_path),
        cv2.VideoWriter_fourcc(*"MJPG"),
        args.fps,
        (w, h),
    )
    print(f"Camera ready: {w}x{h} @ {args.fps} fps → {video_path}")

    # ------------------------------------------------------------------
    # Sonar setup and recording thread
    # ------------------------------------------------------------------
    sock = wlsonar.open_sonar_udp_multicast_socket(
        mcast_group=args.sonar_multicast,
        udp_port=args.sonar_port,
    )
    sock.settimeout(1.0)

    sonar_timestamps: list[tuple] = []
    sonar_file = open(sonar_path, "wb")

    def sonar_thread() -> None:
        packet_idx = 0
        while not stop_event.is_set():
            try:
                data, _addr = sock.recvfrom(wlsonar.UDP_MAX_DATAGRAM_SIZE)
            except TimeoutError:
                continue
            except OSError:
                break  # socket closed

            wall_ts = time.time()
            sonar_file.write(data)
            sonar_file.flush()

            try:
                msg = rip.unpackb(data)
            except rip.UnknownProtobufTypeError:
                packet_idx += 1
                continue
            except Exception:
                packet_idx += 1
                continue

            # Extract sonar-internal timestamp and sequence id
            hdr = msg.header
            sonar_ts = hdr.timestamp.seconds + hdr.timestamp.nanos / 1e9
            seq_id = hdr.sequence_id
            sonar_timestamps.append((packet_idx, seq_id, sonar_ts, wall_ts))

            # Update shared display image
            img = _sonar_msg_to_display(msg)
            if img is not None:
                with sonar_lock:
                    latest_sonar[0] = img

            packet_idx += 1

    sonar_th = threading.Thread(target=sonar_thread, daemon=True, name="sonar-thread")
    sonar_th.start()
    print(f"Sonar listening on {args.sonar_multicast}:{args.sonar_port} → {sonar_path}")
    print()
    print("Recording started.  Press 'q' in the preview window to stop.")

    # ------------------------------------------------------------------
    # Main loop – camera recording + live preview
    # ------------------------------------------------------------------
    cam_timestamps: list[tuple[int, float]] = []
    frame_idx = 0

    # Write the first frame we already captured
    writer.write(first_frame)
    cam_timestamps.append((frame_idx, time.time()))
    frame_idx += 1

    try:
        while True:
            ok, frame, wall_ts = cam.read(timeout_ms=50)
            if ok and frame is not None:
                writer.write(frame)
                cam_timestamps.append((frame_idx, wall_ts))
                frame_idx += 1

            # Build side-by-side display (camera | sonar)
            display = first_frame if frame is None else frame
            disp_h, disp_w = display.shape[:2]

            with sonar_lock:
                sonar_img = latest_sonar[0]

            if sonar_img is not None:
                sonar_bgr = cv2.applyColorMap(
                    cv2.resize(sonar_img, (disp_w, disp_h)),
                    cv2.COLORMAP_OCEAN,
                )
            else:
                sonar_bgr = np.zeros((disp_h, disp_w, 3), dtype=np.uint8)
                cv2.putText(
                    sonar_bgr, "Waiting for sonar...",
                    (20, disp_h // 2), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (80, 80, 80), 1,
                )

            combined = np.hstack([display, sonar_bgr])
            cv2.putText(combined, "Camera",        (10, 28),           cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 230, 0), 2)
            cv2.putText(combined, "Sonar",         (disp_w + 10, 28),  cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 230, 0), 2)
            cv2.putText(
                combined,
                f"Frame {frame_idx}  |  Sonar pkts {len(sonar_timestamps)}  |  Press Q to stop",
                (10, combined.shape[0] - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1,
            )
            cv2.imshow("Sync Recorder", combined)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):  # q, Q, or Esc
                break

    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping – finalising files...")
        stop_event.set()
        sonar_th.join(timeout=3.0)

        cam.stop()
        writer.release()
        sonar_file.close()
        sock.close()
        cv2.destroyAllWindows()

        # Save timestamp CSVs
        with open(cam_csv_path, "w", newline="") as f:
            w_csv = csv.writer(f)
            w_csv.writerow(["frame_index", "wall_clock_sec"])
            w_csv.writerows(cam_timestamps)

        with open(son_csv_path, "w", newline="") as f:
            w_csv = csv.writer(f)
            w_csv.writerow(["packet_index", "sequence_id", "sonar_timestamp_sec", "wall_clock_sec"])
            w_csv.writerows(sonar_timestamps)

        print(f"Camera  : {frame_idx} frames → {video_path}")
        print(f"Sonar   : {len(sonar_timestamps)} packets → {sonar_path}")
        print(f"Timestamps saved to {cam_csv_path.name} and {son_csv_path.name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synchronized BlueROV2 camera + WaterLinked Sonar 3D-15 recorder"
    )
    parser.add_argument("--camera-port",    type=int,   default=5604,         help="UDP port for camera MJPEG stream (default: 5604)")
    parser.add_argument("--camera-payload", type=int,   default=26,           help="RTP payload type for camera (default: 26)")
    parser.add_argument("--fps",            type=float, default=30.0,         help="Camera frame rate for VideoWriter (default: 30)")
    parser.add_argument("--sonar-port",     type=int,   default=4747,         help="UDP port for sonar multicast (default: 4747)")
    parser.add_argument("--sonar-multicast",            default="224.0.0.96", help="Sonar multicast group (default: 224.0.0.96)")
    parser.add_argument("--output-dir",                 default="recordings", help="Directory for output files (default: recordings/)")
    args = parser.parse_args()
    record(args)


if __name__ == "__main__":
    main()
