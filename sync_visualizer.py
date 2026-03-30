#!/usr/bin/env python3
"""
Synchronized visualizer for BlueROV2 camera video and WaterLinked Sonar 3D-15 recording.

Reads the camera .mkv and sonar .sonar files produced by sync_recorder.py, aligns
them by wall-clock timestamps, and plays back both streams side-by-side.

Usage:
    python sync_visualizer.py \\
        --camera   recordings/camera_20260327_120000.mkv \\
        --sonar    recordings/sonar_20260327_120000.sonar \\
        --cam-ts   recordings/camera_20260327_120000_timestamps.csv \\
        --sonar-ts recordings/sonar_20260327_120000_timestamps.csv

    Optional:
        --show-3d          Open a matplotlib window with a live 3D point cloud
        --colormap OCEAN   OpenCV colormap name for sonar image (default: OCEAN)

Keyboard controls (click the preview window first):
    Q / Esc    quit
    SPACE      pause / resume
    →  / D     seek forward  5 s
    ←  / A     seek backward 5 s
    + / =      double playback speed (max 8×)
    -          halve playback speed (min 0.25×)
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import cv2
import numpy as np

_wlsonar_src = Path(__file__).parent / "wlsonar" / "src"
if _wlsonar_src.exists() and str(_wlsonar_src) not in sys.path:
    sys.path.insert(0, str(_wlsonar_src))

import wlsonar                              # noqa: E402
import wlsonar.range_image_protocol as rip  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_timestamps(csv_path: str) -> list[dict]:
    rows = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append({k: (float(v) if v not in ("", "None") else None)
                         for k, v in row.items()})
    return rows


def _load_sonar_packets(sonar_path: str) -> list:
    """Load all sonar packets into a list.  Skips unknown / corrupt packets."""
    packets = []
    with open(sonar_path, "rb") as f:
        while True:
            try:
                packets.append(rip.unpack(f))
            except EOFError:
                break
            except Exception:
                continue
    return packets


COLORMAPS = {
    "OCEAN":   cv2.COLORMAP_OCEAN,
    "JET":     cv2.COLORMAP_JET,
    "HOT":     cv2.COLORMAP_HOT,
    "VIRIDIS": cv2.COLORMAP_VIRIDIS,
    "BONE":    cv2.COLORMAP_BONE,
}


def _sonar_to_bgr(msg, colormap_code: int) -> np.ndarray | None:
    """Render a sonar protobuf message as a BGR colour image."""
    try:
        if isinstance(msg, rip.BitmapImageGreyscale8):
            grey = np.frombuffer(msg.image_pixel_data, dtype=np.uint8).reshape(
                msg.height, msg.width
            )
            return cv2.applyColorMap(cv2.flip(grey, 0), colormap_code)

        if isinstance(msg, rip.RangeImage):
            px = np.array(msg.image_pixel_data, dtype=np.float32)
            if px.max() > 0:
                px = (px / px.max() * 255).astype(np.uint8)
            else:
                px = px.astype(np.uint8)
            return cv2.applyColorMap(cv2.flip(px.reshape(msg.height, msg.width), 0), colormap_code)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Main visualisation loop
# ---------------------------------------------------------------------------

def visualize(args: argparse.Namespace) -> None:
    colormap_code = COLORMAPS.get(args.colormap.upper(), cv2.COLORMAP_OCEAN)

    # Load timestamps
    print("Loading timestamps...")
    cam_ts_rows  = _load_timestamps(args.cam_ts)
    son_ts_rows  = _load_timestamps(args.sonar_ts)

    cam_wall  = np.array([r["wall_clock_sec"] for r in cam_ts_rows])
    son_wall  = np.array([r["wall_clock_sec"] for r in son_ts_rows])

    # Normalise both clocks to a shared t=0
    t0 = min(cam_wall[0], son_wall[0])
    cam_wall -= t0
    son_wall -= t0
    recording_duration = max(cam_wall[-1], son_wall[-1])

    # Load sonar packets
    print(f"Loading sonar packets from {args.sonar} ...")
    sonar_packets = _load_sonar_packets(args.sonar)
    print(f"  {len(sonar_packets)} packets loaded")

    # Open camera video
    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f"ERROR: Cannot open {args.camera}")
        return
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    print(f"  Camera: {total_frames} frames @ {fps:.1f} fps, duration {recording_duration:.1f} s")

    # Optional 3-D point cloud window
    fig3d = ax3d = None
    if args.show_3d:
        try:
            import matplotlib
            matplotlib.use("TkAgg")        # non-blocking backend
            import matplotlib.pyplot as plt
            from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
            plt.ion()
            fig3d, ax3d = plt.subplots(1, 1, figsize=(6, 6),
                                       subplot_kw={"projection": "3d"})
            ax3d.set_xlabel("X (m)")
            ax3d.set_ylabel("Y (m)")
            ax3d.set_zlabel("Z (m)")
            ax3d.set_title("Sonar 3D Point Cloud")
            print("  3D window opened (matplotlib)")
        except Exception as e:
            print(f"  WARNING: Could not open 3D window: {e}")
            fig3d = ax3d = None

    # Playback state
    paused            = False
    playback_speed    = 1.0
    data_time         = 0.0          # current position in normalised time (seconds)
    real_clock_start  = time.time()  # real-world time when this data_time was set
    last_frame        = None
    last_sonar_bgr    = None

    print("\nControls: Q=quit  SPACE=pause  ←/→=seek 5 s  +/-=speed\n")

    while True:
        if not paused:
            elapsed_real = (time.time() - real_clock_start) * playback_speed
            data_time_now = data_time + elapsed_real
            data_time_now = min(data_time_now, recording_duration)

            # Find camera frame index closest to data_time_now
            cam_idx = int(np.searchsorted(cam_wall, data_time_now, side="right")) - 1
            cam_idx = int(np.clip(cam_idx, 0, total_frames - 1))

            cap.set(cv2.CAP_PROP_POS_FRAMES, cam_idx)
            ret, frame = cap.read()
            if ret:
                last_frame = frame

            # Find sonar packet closest to data_time_now
            if len(son_wall) > 0:
                son_idx = int(np.argmin(np.abs(son_wall - data_time_now)))
                if son_idx < len(sonar_packets):
                    msg = sonar_packets[son_idx]
                    bgr = _sonar_to_bgr(msg, colormap_code)
                    if bgr is not None:
                        last_sonar_bgr = bgr

                    # Update 3D cloud (only for RangeImage, every N frames to stay responsive)
                    if fig3d is not None and isinstance(msg, rip.RangeImage):
                        try:
                            pts = wlsonar.range_image_to_xyz(msg)
                            if pts:
                                xyz = np.array(pts, dtype=np.float32)
                                ax3d.cla()
                                ax3d.scatter(xyz[:, 0], xyz[:, 1], xyz[:, 2],
                                             s=1, c="cyan", alpha=0.6)
                                ax3d.set_xlabel("X (m)")
                                ax3d.set_ylabel("Y (m)")
                                ax3d.set_zlabel("Z (m)")
                                fig3d.canvas.draw()
                                fig3d.canvas.flush_events()
                        except Exception:
                            pass

            if data_time_now >= recording_duration:
                print("End of recording.")
                break

        # --- Build combined display ---
        if last_frame is None:
            time.sleep(0.01)
            continue

        dh, dw = last_frame.shape[:2]

        if last_sonar_bgr is not None:
            sonar_panel = cv2.resize(last_sonar_bgr, (dw, dh))
        else:
            sonar_panel = np.zeros((dh, dw, 3), dtype=np.uint8)
            cv2.putText(sonar_panel, "No sonar data",
                        (20, dh // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (80, 80, 80), 1)

        combined = np.hstack([last_frame, sonar_panel])

        # Labels
        status_str = "PAUSED" if paused else f"{playback_speed:.2g}x"
        t_disp = data_time if paused else (data_time + (time.time() - real_clock_start) * playback_speed)
        t_disp = min(t_disp, recording_duration)

        cv2.putText(combined, "Camera",         (10, 28),          cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 230, 0), 2)
        cv2.putText(combined, "Sonar",          (dw + 10, 28),     cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 230, 0), 2)
        cv2.putText(
            combined,
            f"T={t_disp:.2f}s / {recording_duration:.1f}s   [{status_str}]   Q=quit  SPACE=pause",
            (10, combined.shape[0] - 8),
            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1,
        )

        cv2.imshow("Synchronized Playback", combined)

        key = cv2.waitKey(max(1, int(1000 / fps / playback_speed / 2))) & 0xFF

        if key in (ord("q"), ord("Q"), 27):
            break

        elif key == ord(" "):
            if paused:
                # Resume: reset the real-clock baseline from current position
                real_clock_start = time.time()
            else:
                # Pause: commit current data time
                data_time = data_time + (time.time() - real_clock_start) * playback_speed
                data_time = float(np.clip(data_time, 0, recording_duration))
            paused = not paused

        elif key in (83, ord("d")):  # RIGHT arrow or D  → seek forward 5 s
            data_time = float(np.clip(
                (data_time + (time.time() - real_clock_start) * playback_speed) + 5.0,
                0, recording_duration,
            ))
            real_clock_start = time.time()
            paused = False

        elif key in (81, ord("a")):  # LEFT arrow or A  → seek backward 5 s
            data_time = float(np.clip(
                (data_time + (time.time() - real_clock_start) * playback_speed) - 5.0,
                0, recording_duration,
            ))
            real_clock_start = time.time()
            paused = False

        elif key in (ord("+"), ord("=")):  # speed up
            data_time = float(np.clip(
                data_time + (time.time() - real_clock_start) * playback_speed,
                0, recording_duration,
            ))
            playback_speed = min(8.0, playback_speed * 2)
            real_clock_start = time.time()

        elif key == ord("-"):             # slow down
            data_time = float(np.clip(
                data_time + (time.time() - real_clock_start) * playback_speed,
                0, recording_duration,
            ))
            playback_speed = max(0.25, playback_speed / 2)
            real_clock_start = time.time()

    cap.release()
    cv2.destroyAllWindows()
    if fig3d is not None:
        import matplotlib.pyplot as plt
        plt.close(fig3d)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synchronized BlueROV2 camera + WaterLinked Sonar 3D-15 visualizer"
    )
    parser.add_argument("--camera",    required=True, help="Camera video file (.mkv)")
    parser.add_argument("--sonar",     required=True, help="Sonar recording file (.sonar)")
    parser.add_argument("--cam-ts",    required=True, help="Camera timestamps CSV")
    parser.add_argument("--sonar-ts",  required=True, help="Sonar timestamps CSV")
    parser.add_argument("--show-3d",   action="store_true",
                        help="Open a matplotlib window with live 3D point cloud (requires RangeImage packets)")
    parser.add_argument("--colormap",  default="OCEAN",
                        choices=list(COLORMAPS), metavar="|".join(COLORMAPS),
                        help="Colour map for sonar image (default: OCEAN)")
    args = parser.parse_args()
    visualize(args)


if __name__ == "__main__":
    main()
