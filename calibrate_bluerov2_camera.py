#!/usr/bin/env python3
"""BlueROV2 ChArUco 相机标定工具。
支持: UDP 5600 实时流 | 视频文件 | 图片目录
默认自动检测 ChArUco 板并截帧，晃动板子即可。
"""

import os
import sys
import argparse
import glob
import time
import threading
import subprocess
import signal
import shutil
import yaml
import cv2
import numpy as np

_script_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_script_dir)
sys.path.insert(0, _project_root)

DEFAULT_SQUARES_X = 14
DEFAULT_SQUARES_Y = 11
DEFAULT_SQUARE_LENGTH_M = 0.035
DEFAULT_MARKER_LENGTH_M = 0.025


# ── GStreamer UDP 接收 ──────────────────────────────────────────────

class GStreamerUDPCapture:
    """用 gst-launch-1.0 子进程接收 BlueROV2 UDP RTP H.264，pipe rawvideo 给 Python。
    
    优化：只保留最新帧，避免缓冲区堆积导致延迟。
    """

    def __init__(self, port=5600, width=1920, height=1080, timeout_sec=20):
        self.port = port
        self.w = width
        self.h = height
        self.frame_size = width * height * 3
        self._opened = False
        self._latest_frame = None  # 只保留最新一帧
        self._frame_ready = False
        self._frame_id = 0  # 帧计数器
        self._last_read_id = -1  # 上次读取的帧 ID
        self._lock = threading.Lock()
        self._stop = False

        gst_bin = shutil.which("gst-launch-1.0")
        if gst_bin is None:
            print("[UDP] 错误: 找不到 gst-launch-1.0，请安装 GStreamer")
            return

        # 低延迟优化管道:
        # - udpsrc buffer-size: 减小接收缓冲
        # - rtpjitterbuffer latency=0: 禁用抖动缓冲
        # - avdec_h264 参数: 低延迟解码
        # - queue max-size-buffers=1: 最多缓冲1帧
        # - sync=false: 不等待时间戳同步
        pipeline = (
            f"gst-launch-1.0 -q -e "
            f"udpsrc port={port} buffer-size=65536 ! "
            f"'application/x-rtp,payload=96' ! "
            f"rtpjitterbuffer latency=0 ! "
            f"rtph264depay ! h264parse ! "
            f"avdec_h264 max-threads=4 output-corrupt=false ! "
            f"queue max-size-buffers=1 leaky=downstream ! "
            f"videoconvert ! 'video/x-raw,format=BGR' ! "
            f"fdsink fd=1 sync=false"
        )
        try:
            self._proc = subprocess.Popen(
                pipeline, shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except Exception as e:
            print(f"[UDP] 启动 GStreamer 失败: {e}")
            return

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        print(f"[UDP] 等待 gst-launch-1.0 接收第一帧 (最长 {timeout_sec}s) ...")
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            with self._lock:
                if self._frame_ready:
                    break
            time.sleep(0.2)

        with self._lock:
            if self._frame_ready:
                self._opened = True
                print(f"[UDP] 成功! 分辨率 {self.w}x{self.h}")
            else:
                print("[UDP] 超时: 未收到视频帧")

    def _reader_loop(self):
        """后台线程：持续读取帧，只保留最新一帧（丢弃旧帧避免延迟）。"""
        buf = b""
        while not self._stop:
            try:
                # 每次读取一个帧大小的数据
                chunk = self._proc.stdout.read(self.frame_size - len(buf))
            except (ValueError, OSError):
                break
            if not chunk:
                break
            buf += chunk
            
            # 当凑够一帧时，更新最新帧
            while len(buf) >= self.frame_size:
                frame_data = buf[:self.frame_size]
                buf = buf[self.frame_size:]
                frame = np.frombuffer(frame_data, dtype=np.uint8).reshape((self.h, self.w, 3))
                with self._lock:
                    self._latest_frame = frame.copy()
                    self._frame_ready = True
                    self._frame_id += 1

    def isOpened(self):
        return self._opened

    def read(self):
        """获取最新帧（兼容 cv2.VideoCapture 接口）。"""
        with self._lock:
            if not self._frame_ready or self._latest_frame is None:
                return False, None
            self._last_read_id = self._frame_id
            return True, self._latest_frame.copy()

    def read_new(self, timeout_ms=100):
        """等待并获取新帧（避免重复处理同一帧）。
        
        Returns:
            (success, frame, is_new) - is_new 表示是否是新帧
        """
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            with self._lock:
                if self._frame_ready and self._latest_frame is not None:
                    if self._frame_id != self._last_read_id:
                        self._last_read_id = self._frame_id
                        return True, self._latest_frame.copy(), True
            time.sleep(0.001)  # 1ms
        # 超时返回上一帧
        with self._lock:
            if self._frame_ready and self._latest_frame is not None:
                return True, self._latest_frame.copy(), False
        return False, None, False

    def get_frame_id(self):
        """获取当前帧 ID（用于检测是否有新帧）。"""
        with self._lock:
            return self._frame_id

    def release(self):
        self._stop = True
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._opened = False


def open_udp_stream(port: int = 5600, timeout_sec: int = 20):
    print(f"[UDP] 正在连接 BlueROV2 视频流 (port {port}) ...")
    print(f"[UDP] 请确保 BlueROV2 已开机且 Cockpit/QGC 能看到视频")
    return GStreamerUDPCapture(port, timeout_sec=timeout_sec)


# ── ChArUco 检测 (兼容新旧 OpenCV API) ──────────────────────────────

def _detect_charuco_corners(gray, board, aruco_dict):
    """兼容新旧 OpenCV ArUco API，返回 (marker_corners, marker_ids, charuco_corners, charuco_ids)。"""
    if hasattr(cv2.aruco, "CharucoDetector"):
        charuco_det = cv2.aruco.CharucoDetector(board)
        charuco_corners, charuco_ids, marker_corners, marker_ids = charuco_det.detectBoard(gray)
        if charuco_corners is not None and len(charuco_corners) == 0:
            charuco_corners = None
            charuco_ids = None
    else:
        if hasattr(cv2.aruco, "DetectorParameters_create"):
            cp = cv2.aruco.DetectorParameters_create()
        else:
            cp = cv2.aruco.DetectorParameters()
        if hasattr(cv2.aruco, "ArucoDetector"):
            det = cv2.aruco.ArucoDetector(aruco_dict, cp)
            marker_corners, marker_ids, _ = det.detectMarkers(gray)
        else:
            marker_corners, marker_ids, _ = cv2.aruco.detectMarkers(gray, aruco_dict, parameters=cp)
        charuco_corners, charuco_ids = None, None
        if marker_ids is not None and len(marker_ids) >= 4:
            ret_c, charuco_corners, charuco_ids = cv2.aruco.interpolateCornersCharuco(
                marker_corners, marker_ids, gray, board
            )
    return marker_corners, marker_ids, charuco_corners, charuco_ids


# ── 帧采集 ──────────────────────────────────────────────────────────

def collect_frames_auto(cap, board, aruco_dict, num_frames: int,
                        min_interval_sec: float = 1.5, min_corners: int = 6):
    """自动检测 ChArUco 并采集：晃动板子即可，不需要按键。按 q 提前结束。"""
    frames = []
    last_capture_time = 0.0
    frame_count = 0
    log_interval = 30

    print(f"[Auto] 自动采集模式: 对准 ChArUco 板，慢慢晃动，自动截帧")
    print(f"[Auto] 需要 {num_frames} 帧，每帧间隔 >= {min_interval_sec}s，按 q 提前结束")

    while len(frames) < num_frames:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.01)
            continue
        frame_count += 1
        now = time.time()

        vis = frame.copy()
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        marker_corners, marker_ids, charuco_corners, charuco_ids = \
            _detect_charuco_corners(gray, board, aruco_dict)
        n_markers = 0 if marker_ids is None else len(marker_ids)

        detected = False
        n_corners = 0
        if marker_ids is not None and n_markers >= 1:
            cv2.aruco.drawDetectedMarkers(vis, marker_corners, marker_ids)
        if charuco_corners is not None:
            n_corners = len(charuco_corners)
            if n_corners >= min_corners:
                cv2.aruco.drawDetectedCornersCharuco(vis, charuco_corners, charuco_ids)
                detected = True

        if frame_count % log_interval == 0:
            print(f"  [Debug] 第 {frame_count} 帧: {n_markers} markers, {n_corners} corners")

        status_color = (0, 255, 0) if detected else (0, 0, 255)
        cv2.putText(vis,
                    f"Markers: {n_markers}  Corners: {n_corners}  Captured: {len(frames)}/{num_frames}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, status_color, 2)

        if detected and (now - last_capture_time) >= min_interval_sec:
            frames.append(frame.copy())
            last_capture_time = now
            print(f"  [Auto] Captured {len(frames)}/{num_frames}  "
                  f"({n_markers} markers, {n_corners} corners)")
            cv2.putText(vis, "CAPTURED!", (10, 70),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)

        cv2.imshow("ChArUco Auto Calibration - press 'q' to quit", vis)
        k = cv2.waitKey(1) & 0xFF
        if k == ord("q"):
            break

    cv2.destroyAllWindows()
    print(f"[Auto] 采集完成, 共 {len(frames)} 帧")
    return frames


def collect_frames_manual(cap, num_frames: int):
    """手动按 s 截帧，按 q 退出。"""
    frames = []
    while len(frames) < num_frames:
        ret, frame = cap.read()
        if not ret:
            break
        cv2.imshow("Calibration - press 's' to capture, 'q' to quit", frame)
        k = cv2.waitKey(1) & 0xFF
        if k == ord("q"):
            break
        if k == ord("s"):
            frames.append(frame.copy())
            print(f"  Captured {len(frames)}/{num_frames}")
    cv2.destroyAllWindows()
    return frames


def collect_frames_from_dir(path: str, exts=("*.jpg", "*.png", "*.jpeg")):
    frames = []
    for ext in exts:
        for f in sorted(glob.glob(os.path.join(path, ext))):
            im = cv2.imread(f)
            if im is not None:
                frames.append(im)
    return frames


# ── 检测 & 标定 ─────────────────────────────────────────────────────

def detect_charuco(image, board, aruco_dict, all_charuco_corners, all_charuco_ids, frame_idx=0):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    marker_corners, marker_ids, charuco_corners, charuco_ids = \
        _detect_charuco_corners(gray, board, aruco_dict)
    n_markers = 0 if marker_ids is None else len(marker_ids)
    if marker_ids is None or n_markers < 4:
        print(f"  帧 {frame_idx}: 检测到 {n_markers} 个 ArUco marker (需要 >= 4)，跳过")
        return False
    n_corners = 0 if charuco_corners is None else len(charuco_corners)
    if n_corners < 4:
        print(f"  帧 {frame_idx}: {n_markers} markers, 但仅插值出 {n_corners} 个角点 (需要 >= 4)，跳过")
        return False
    all_charuco_corners.append(charuco_corners)
    all_charuco_ids.append(charuco_ids)
    print(f"  帧 {frame_idx}: OK - {n_markers} markers, {n_corners} 角点")
    return True


def run_calibration(board, image_size, all_charuco_corners, all_charuco_ids):
    camera_matrix = np.eye(3)
    dist_coeffs = np.zeros(5)
    obj_points = []
    for corners, ids in zip(all_charuco_corners, all_charuco_ids):
        obj_pts = board.getChessboardCorners()
        ids_flat = ids.flatten()
        obj_points.append(obj_pts[ids_flat])

    rep_err, camera_matrix, dist_coeffs, rvecs, tvecs = cv2.calibrateCamera(
        obj_points, all_charuco_corners, image_size, camera_matrix, dist_coeffs,
    )
    return camera_matrix, dist_coeffs, rep_err


def write_orbslam3_yaml(out_path, camera_matrix, dist_coeffs, image_size, rep_err):
    K = camera_matrix
    d = dist_coeffs.flatten()
    w, h = image_size
    data = {
        "Camera.type": "PinHole",
        "Camera.fx": float(K[0, 0]),
        "Camera.fy": float(K[1, 1]),
        "Camera.cx": float(K[0, 2]),
        "Camera.cy": float(K[1, 2]),
        "Camera.k1": float(d[0]) if len(d) > 0 else 0.0,
        "Camera.k2": float(d[1]) if len(d) > 1 else 0.0,
        "Camera.p1": float(d[2]) if len(d) > 2 else 0.0,
        "Camera.p2": float(d[3]) if len(d) > 3 else 0.0,
        "Camera.width": w,
        "Camera.height": h,
        "Camera.fps": 30,
        "Camera.RGB": 1,
        "Reprojection.error": float(rep_err),
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    print(f"[OK] 已写入 {out_path}")


# ── main ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="BlueROV2 ChArUco 相机标定")
    parser.add_argument("input", nargs="?", default="udp",
                        help="输入: udp | 视频路径 | 图片目录")
    parser.add_argument("--squares-x", type=int, default=DEFAULT_SQUARES_X,
                        help=f"ChArUco 横向格子数 (默认 {DEFAULT_SQUARES_X})")
    parser.add_argument("--squares-y", type=int, default=DEFAULT_SQUARES_Y,
                        help=f"ChArUco 纵向格子数 (默认 {DEFAULT_SQUARES_Y})")
    parser.add_argument("--square-length", type=float, default=DEFAULT_SQUARE_LENGTH_M,
                        help=f"方格边长 (米) (默认 {DEFAULT_SQUARE_LENGTH_M})")
    parser.add_argument("--marker-length", type=float, default=DEFAULT_MARKER_LENGTH_M,
                        help=f"ArUco marker 边长 (米) (默认 {DEFAULT_MARKER_LENGTH_M})")
    parser.add_argument("--num-frames", type=int, default=15,
                        help="采集帧数 (默认 15)")
    parser.add_argument("--output", "-o",
                        default=os.path.join(_project_root, "config", "bluerov2_calibrated_2.yaml"),
                        help="输出 YAML 路径")
    parser.add_argument("--manual", action="store_true",
                        help="手动按键截帧模式 (默认自动检测)")
    parser.add_argument("--interval", type=float, default=1.5,
                        help="自动模式截帧最小间隔秒数 (默认 1.5)")
    parser.add_argument("--dict", default="DICT_6X6_250",
                        help="ArUco 字典 (默认 DICT_6X6_250)")
    args = parser.parse_args()

    # ArUco 字典
    dict_name = args.dict.upper()
    if not hasattr(cv2.aruco, dict_name):
        print(f"错误: 未知 ArUco 字典 '{dict_name}'")
        sys.exit(1)
    aruco_dict = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dict_name))
    n_markers_needed = (args.squares_x * args.squares_y) // 2
    print(f"[Board] {args.squares_x}x{args.squares_y}, "
          f"square={args.square_length*1000:.0f}mm, marker={args.marker_length*1000:.0f}mm")
    print(f"[Board] 字典: {dict_name}, 需要 {n_markers_needed} 个 marker")

    board = cv2.aruco.CharucoBoard(
        (args.squares_x, args.squares_y),
        args.square_length,
        args.marker_length,
        aruco_dict,
    )
    if hasattr(board, "setLegacyPattern"):
        board.setLegacyPattern(True)

    # 收集图像
    if os.path.isdir(args.input):
        print(f"[Input] 图片目录: {args.input}")
        frames = collect_frames_from_dir(args.input)
    elif args.input.lower() in ("udp", "5600"):
        print("[Input] UDP 5600 (H.264)")
        cap = open_udp_stream(5600, timeout_sec=20)
        if not cap.isOpened():
            print("=" * 60)
            print("错误: 无法打开 UDP 5600 视频流")
            print()
            print("请检查:")
            print("  1. BlueROV2 已开机 (ping 192.168.2.2)")
            print("  2. Cockpit/QGC 可以看到视频")
            print("  3. GStreamer 已安装 (gst-launch-1.0 --version)")
            print()
            print("或者用视频文件:")
            print("  python calibrate_bluerov2_camera.py /path/to/video.mp4")
            print("=" * 60)
            sys.exit(1)
        if args.manual:
            frames = collect_frames_manual(cap, args.num_frames)
        else:
            frames = collect_frames_auto(cap, board, aruco_dict, args.num_frames,
                                         min_interval_sec=args.interval)
        cap.release()
    else:
        if not os.path.isfile(args.input):
            print(f"错误: 文件不存在 {args.input}")
            sys.exit(1)
        print(f"[Input] 视频: {args.input}")
        cap = cv2.VideoCapture(args.input)
        if args.manual:
            frames = collect_frames_manual(cap, args.num_frames)
        else:
            frames = collect_frames_auto(cap, board, aruco_dict, args.num_frames,
                                         min_interval_sec=args.interval)
        cap.release()

    if len(frames) < 3:
        print("错误: 至少需要 3 张有效图像")
        sys.exit(1)

    h, w = frames[0].shape[:2]
    image_size = (w, h)

    all_charuco_corners = []
    all_charuco_ids = []
    print(f"\n[Detect] 正在检测 ChArUco 角点 ({len(frames)} 帧) ...")
    for i, im in enumerate(frames):
        detect_charuco(im, board, aruco_dict, all_charuco_corners, all_charuco_ids, frame_idx=i)
    print(f"[Detect] 成功检测: {len(all_charuco_corners)}/{len(frames)} 帧")

    if len(all_charuco_corners) < 3:
        print("=" * 60)
        print(f"错误: 至少需要 3 帧成功检测到 ChArUco 角点 (当前 {len(all_charuco_corners)})")
        print()
        print("可能原因:")
        print("  1. 板子参数不匹配 (格子数、ArUco 字典)")
        print(f"     当前: {args.squares_x}x{args.squares_y}, {dict_name}")
        print("  2. 图像太模糊或光线太暗")
        print("  3. 板子距离太远或角度太大")
        print("=" * 60)
        sys.exit(1)

    camera_matrix, dist_coeffs, rep_err = run_calibration(
        board, image_size, all_charuco_corners, all_charuco_ids,
    )
    print(f"[Calibration] 重投影误差: {rep_err:.4f}")
    print(f"  fx={camera_matrix[0,0]:.2f}  fy={camera_matrix[1,1]:.2f}")
    print(f"  cx={camera_matrix[0,2]:.2f}  cy={camera_matrix[1,2]:.2f}")

    write_orbslam3_yaml(args.output, camera_matrix, dist_coeffs, image_size, rep_err)


if __name__ == "__main__":
    main()
