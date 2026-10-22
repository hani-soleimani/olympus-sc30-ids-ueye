r"""Live Olympus SC30 IDS acquisition, bead tracking, and annotated MP4 recording.

Run from the project root after closing DP2 and any other camera client:
    .\.venv\Scripts\python.exe tracking\ids_ueye_live_bead_tracking.py

Choose an ROI on the first frame, then click bead centers (Enter to accept,
right-click to undo, Esc to cancel). Press Q/Esc in the live window to finish.
Camera settings are restored on exit.
"""
from __future__ import annotations

import argparse
import csv
import ctypes
from datetime import datetime
import math
from pathlib import Path
import statistics
import sys
import time
from ctypes import c_char, c_int, c_int32, c_uint, c_uint32, c_void_p

import cv2
import numpy as np

DLL_PATH = r"C:\Windows\System32\uc480_64.dll"
TARGET_SERIAL = "0057004412"
IS_SUCCESS = 0
IS_DONT_WAIT = 0
IS_WAIT = 1
IS_AOI_IMAGE_GET_AOI = 2
IS_AOI_IMAGE_SET_AOI = 1
IS_BINNING_2X_VERTICAL = 0x0001
IS_BINNING_2X_HORIZONTAL = 0x0002
IS_GET_BINNING = 0x8000
IS_GET_SUPPORTED_BINNING = 0x8001
IS_EXPOSURE_GET = 7
IS_EXPOSURE_SET = 12
IS_PIXELCLOCK_CMD_GET_NUMBER = 1
IS_PIXELCLOCK_CMD_GET_LIST = 2
IS_PIXELCLOCK_CMD_GET = 5
IS_PIXELCLOCK_CMD_SET = 6
IS_GET_FRAMERATE = 0x8000
BUFFER_COUNT = 8
MAX_MISSED_FRAMES = 5


class CameraInfo(ctypes.Structure):
    _fields_ = [
        ("dwCameraID", c_uint32), ("dwDeviceID", c_uint32),
        ("dwSensorID", c_uint32), ("dwInUse", c_uint32),
        ("SerNo", c_char * 16), ("Model", c_char * 16),
        ("dwStatus", c_uint32), ("dwReserved", c_uint32 * 2),
        ("FullModelName", c_char * 32), ("dwReserved2", c_uint32 * 5),
    ]


class CameraListHead(ctypes.Structure):
    _fields_ = [("dwCount", c_uint32)]


class Rect(ctypes.Structure):
    _fields_ = [("s32X", c_int32), ("s32Y", c_int32),
                ("s32Width", c_int32), ("s32Height", c_int32)]


def bind(dll, name, argtypes, restype=c_int):
    fn = getattr(dll, name)
    fn.argtypes = argtypes
    fn.restype = restype
    return fn


def check(name, code):
    if code != IS_SUCCESS:
        raise RuntimeError(f"{name} returned IDS error {code}")


def decode(value):
    return value.split(b"\0", 1)[0].decode("ascii", "replace")


def create_detector():
    params = cv2.SimpleBlobDetector_Params()
    params.minThreshold = 0
    params.maxThreshold = 256
    params.thresholdStep = 10
    params.minRepeatability = 2
    params.minDistBetweenBlobs = 5
    params.filterByColor = True
    params.blobColor = 255
    params.filterByArea = False
    params.filterByCircularity = False
    params.filterByConvexity = False
    params.filterByInertia = False
    return cv2.SimpleBlobDetector_create(params)


def detect_candidates(frame, roi, detector, min_size, max_size):
    x, y, w, h = roi
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    points = detector.detect(gray[y:y + h, x:x + w])
    return [
        cv2.KeyPoint(point.pt[0] + x, point.pt[1] + y, point.size)
        for point in points if min_size <= point.size <= max_size
    ]


def choose_seeds(frame, roi, candidates):
    selected = []
    title = "Select beads: left click add, right click undo, Enter accept, Esc cancel"

    def draw():
        view = frame.copy()
        x, y, w, h = roi
        cv2.rectangle(view, (x, y), (x + w - 1, y + h - 1), (255, 255, 0), 2)
        for n, point in enumerate(candidates, 1):
            center = (round(point.pt[0]), round(point.pt[1]))
            cv2.circle(view, center, max(3, round(point.size / 2)), (0, 165, 255), 1)
            cv2.putText(view, str(n), (center[0] + 4, center[1] - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 165, 255), 1, cv2.LINE_AA)
        for n, (x0, y0) in enumerate(selected, 1):
            cv2.drawMarker(view, (x0, y0), (0, 255, 0), cv2.MARKER_CROSS, 12, 2)
            cv2.putText(view, f"T{n}", (x0 + 5, y0 - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1, cv2.LINE_AA)
        cv2.putText(view, f"Candidates: {len(candidates)} | Seeds: {len(selected)}",
                    (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
        return view

    def on_mouse(event, x, y, flags, userdata):
        if event == cv2.EVENT_LBUTTONDOWN:
            selected.append((x, y))
        elif event == cv2.EVENT_RBUTTONDOWN and selected:
            selected.pop()

    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(title, on_mouse)
    print("Click the bead centers you want to track. Enter accepts; right-click undoes; Esc cancels.")
    try:
        while True:
            cv2.imshow(title, draw())
            key = cv2.waitKey(20) & 0xFF
            if key in (10, 13):
                return selected
            if key == 27:
                return None
    finally:
        cv2.destroyWindow(title)


def associate(previous, candidates, max_distance):
    pairs = []
    for bead_id, state in previous.items():
        px, py = state["position"]
        for index, point in enumerate(candidates):
            distance = math.hypot(point.pt[0] - px, point.pt[1] - py)
            if distance <= max_distance:
                pairs.append((distance, bead_id, index))
    pairs.sort()
    assigned_ids, assigned_candidates = set(), set()
    matches = {}
    for _, bead_id, index in pairs:
        if bead_id not in assigned_ids and index not in assigned_candidates:
            matches[bead_id] = candidates[index]
            assigned_ids.add(bead_id)
            assigned_candidates.add(index)

    current = {}
    visible = {}
    for bead_id, point in matches.items():
        state = {"position": point.pt, "missed": 0, "size": point.size}
        current[bead_id] = state
        visible[bead_id] = point
    for bead_id, old in previous.items():
        if bead_id not in current and old["missed"] < MAX_MISSED_FRAMES:
            current[bead_id] = {
                "position": old["position"], "missed": old["missed"] + 1,
                "size": old.get("size", 0),
            }
    return visible, current


def annotate(frame, roi, visible, tracks, frame_no, fps, missed_count):
    view = frame.copy()
    x, y, w, h = roi
    cv2.rectangle(view, (x, y), (x + w - 1, y + h - 1), (255, 255, 0), 2)
    for bead_id, state in tracks.items():
        if bead_id in visible:
            point = visible[bead_id]
            center = (round(point.pt[0]), round(point.pt[1]))
            radius = max(4, round(point.size / 2))
            color = (0, 255, 0)
            label = f"T{bead_id}"
        else:
            center = (round(state["position"][0]), round(state["position"][1]))
            radius = 6
            color = (0, 0, 255)
            label = f"T{bead_id} LOST {state['missed']}"
        cv2.circle(view, center, radius, color, 2, cv2.LINE_AA)
        cv2.drawMarker(view, center, color, cv2.MARKER_CROSS, 10, 1, cv2.LINE_AA)
        cv2.putText(view, label, (center[0] + radius + 3, center[1] - 3),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
    cv2.putText(view, f"Frame {frame_no} | tracked {len(visible)}/{len(tracks)} | {fps:.1f} displayed FPS",
                (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
    if missed_count:
        cv2.putText(view, f"Missing: {missed_count}", (12, 51),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1, cv2.LINE_AA)
    return view


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=TARGET_SERIAL)
    parser.add_argument("--exposure-ms", type=float, default=10.0)
    parser.add_argument("--min-size", type=float, default=2.0,
                        help="minimum detected bead diameter in output pixels")
    parser.add_argument("--max-size", type=float, default=21.0,
                        help="maximum detected bead diameter in output pixels")
    parser.add_argument("--match-distance", type=float, default=25.0,
                        help="maximum frame-to-frame association distance in pixels")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="optional duration after bead selection; 0 runs until Q/Esc")
    args = parser.parse_args()
    if args.exposure_ms <= 0 or args.min_size < 0 or args.max_size <= args.min_size:
        parser.error("exposure must be positive and max-size must exceed min-size")
    if args.match_distance <= 0 or args.seconds < 0:
        parser.error("match-distance must be positive and seconds nonnegative")

    output_dir = Path(__file__).resolve().parents[1] / "captures"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    video_path = output_dir / f"live_bead_tracking_{stamp}.mp4"
    csv_path = output_dir / f"live_bead_tracking_{stamp}.csv"

    try:
        dll = ctypes.WinDLL(DLL_PATH)
    except OSError as exc:
        print(f"ERROR loading IDS uEye DLL {DLL_PATH}: {exc}", file=sys.stderr)
        return 2

    get_count = bind(dll, "is_GetNumberOfCameras", [ctypes.POINTER(c_int)])
    get_list = bind(dll, "is_GetCameraList", [c_void_p])
    count = c_int()
    check("is_GetNumberOfCameras", get_count(ctypes.byref(count)))
    if count.value <= 0:
        raise RuntimeError("IDS reports no connected cameras")
    listing = ctypes.create_string_buffer(4 + count.value * ctypes.sizeof(CameraInfo))
    ctypes.cast(listing, ctypes.POINTER(CameraListHead)).contents.dwCount = count.value
    check("is_GetCameraList", get_list(ctypes.byref(listing)))
    infos = ctypes.cast(ctypes.addressof(listing) + 4, ctypes.POINTER(CameraInfo))
    camera = next((infos[i] for i in range(count.value)
                   if decode(infos[i].SerNo) == args.serial), None)
    if camera is None:
        raise RuntimeError(f"camera serial {args.serial} not found")
    if camera.dwInUse:
        raise RuntimeError(f"camera {args.serial} is already in use; close DP2/other clients")

    init = bind(dll, "is_InitCamera", [ctypes.POINTER(c_int), c_void_p])
    exit_camera = bind(dll, "is_ExitCamera", [c_int])
    set_binning = bind(dll, "is_SetBinning", [c_int, c_int])
    pixel_clock = bind(dll, "is_PixelClock", [c_int, c_uint, c_void_p, c_uint])
    set_exposure = bind(dll, "is_Exposure", [c_int, c_uint, c_void_p, c_uint])
    set_fps = bind(dll, "is_SetFrameRate", [c_int, ctypes.c_double, ctypes.POINTER(ctypes.c_double)])
    get_fps = bind(dll, "is_GetFramesPerSecond", [c_int, ctypes.POINTER(ctypes.c_double)])
    get_time_range = bind(dll, "is_GetFrameTimeRange", [c_int, ctypes.POINTER(ctypes.c_double),
                                                         ctypes.POINTER(ctypes.c_double),
                                                         ctypes.POINTER(ctypes.c_double)])
    get_depth = bind(dll, "is_GetColorDepth", [c_int, ctypes.POINTER(c_int), ctypes.POINTER(c_int)])
    get_aoi = bind(dll, "is_AOI", [c_int, c_uint, c_void_p, c_uint])
    alloc = bind(dll, "is_AllocImageMem", [c_int, c_int, c_int, c_int,
                                            ctypes.POINTER(c_void_p), ctypes.POINTER(c_int)])
    add_sequence = bind(dll, "is_AddToSequence", [c_int, c_void_p, c_int])
    clear_sequence = bind(dll, "is_ClearSequence", [c_int])
    free_mem = bind(dll, "is_FreeImageMem", [c_int, c_void_p, c_int])
    capture_video = bind(dll, "is_CaptureVideo", [c_int, c_int])
    stop_video = bind(dll, "is_StopLiveVideo", [c_int, c_int])
    get_active = bind(dll, "is_GetActSeqBuf", [c_int, ctypes.POINTER(c_int),
                                               ctypes.POINTER(c_void_p), ctypes.POINTER(c_void_p)])
    lock_buffer = bind(dll, "is_LockSeqBuf", [c_int, c_int, c_void_p])
    unlock_buffer = bind(dll, "is_UnlockSeqBuf", [c_int, c_int, c_void_p])
    inquire = bind(dll, "is_InquireImageMem", [c_int, c_void_p, c_int,
                                                ctypes.POINTER(c_int), ctypes.POINTER(c_int),
                                                ctypes.POINTER(c_int), ctypes.POINTER(c_int)])

    handle = c_int(int(camera.dwCameraID))
    check("is_InitCamera", init(ctypes.byref(handle), None))
    buffers = []
    live_started = sequence_ready = settings_saved = False
    old_binning = old_aoi = old_clock = old_exposure = old_fps = None

    def read_exposure():
        value = ctypes.c_double()
        check("get exposure", set_exposure(handle, IS_EXPOSURE_GET, ctypes.byref(value), ctypes.sizeof(value)))
        return value.value

    def read_clock():
        value = c_uint()
        check("get pixel clock", pixel_clock(handle, IS_PIXELCLOCK_CMD_GET, ctypes.byref(value), ctypes.sizeof(value)))
        return value.value

    def stop_and_free():
        nonlocal live_started, sequence_ready
        if live_started:
            print(f"is_StopLiveVideo: {stop_video(handle, IS_WAIT)}")
            live_started = False
        if sequence_ready:
            print(f"is_ClearSequence: {clear_sequence(handle)}")
            sequence_ready = False
        for mem, mem_id in buffers:
            rc = free_mem(handle, mem, mem_id.value)
            if rc != IS_SUCCESS:
                print(f"WARNING is_FreeImageMem={rc}")
        buffers.clear()

    result = 0
    try:
        old_binning = set_binning(handle, IS_GET_BINNING)
        supported_binning = set_binning(handle, IS_GET_SUPPORTED_BINNING)
        requested_binning = IS_BINNING_2X_VERTICAL | IS_BINNING_2X_HORIZONTAL
        if old_binning < 0 or supported_binning & requested_binning != requested_binning:
            raise RuntimeError(f"2x2 binning unsupported/query failed: current={old_binning}, mask={supported_binning}")
        old_aoi = Rect()
        check("get original AOI", get_aoi(handle, IS_AOI_IMAGE_GET_AOI,
                                          ctypes.byref(old_aoi), ctypes.sizeof(old_aoi)))
        old_clock = read_clock()
        old_exposure = read_exposure()
        old_rate = ctypes.c_double()
        check("get original FPS", set_fps(handle, ctypes.c_double(IS_GET_FRAMERATE), ctypes.byref(old_rate)))
        old_fps = old_rate.value
        if old_fps <= 0:
            raise RuntimeError("Could not read valid original frame rate; no camera settings changed")
        settings_saved = True
        print(f"Saved settings: binning=0x{old_binning:04X}, AOI={old_aoi.s32Width}x{old_aoi.s32Height}, "
              f"clock={old_clock} MHz, exposure={old_exposure:.3f} ms, FPS={old_fps:.3f}")

        check("set 2x2 binning", set_binning(handle, requested_binning))
        if set_binning(handle, IS_GET_BINNING) != requested_binning:
            raise RuntimeError("2x2 binning readback mismatch")
        nclocks = c_uint()
        check("pixel clock count", pixel_clock(handle, IS_PIXELCLOCK_CMD_GET_NUMBER,
                                               ctypes.byref(nclocks), ctypes.sizeof(nclocks)))
        if not 0 < nclocks.value <= 150:
            raise RuntimeError(f"invalid pixel-clock count {nclocks.value}")
        clocks = (c_uint * nclocks.value)()
        check("pixel clock list", pixel_clock(handle, IS_PIXELCLOCK_CMD_GET_LIST,
                                              clocks, ctypes.sizeof(clocks)))
        if 50 not in list(clocks):
            raise RuntimeError(f"50 MHz not supported in 2x2 mode; available={list(clocks)}")
        clock_value = c_uint(50)
        check("set 50 MHz pixel clock", pixel_clock(handle, IS_PIXELCLOCK_CMD_SET,
                                                    ctypes.byref(clock_value), ctypes.sizeof(clock_value)))
        if read_clock() != 50:
            raise RuntimeError("50 MHz pixel-clock readback mismatch")
        exposure = ctypes.c_double(args.exposure_ms)
        check("set exposure", set_exposure(handle, IS_EXPOSURE_SET, ctypes.byref(exposure), ctypes.sizeof(exposure)))
        actual_exposure = read_exposure()
        min_t, max_t, step_t = ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
        check("get frame-time range", get_time_range(handle, ctypes.byref(min_t),
                                                      ctypes.byref(max_t), ctypes.byref(step_t)))
        target_rate = 1.0 / min_t.value
        applied_rate = ctypes.c_double()
        check("set max frame rate", set_fps(handle, target_rate, ctypes.byref(applied_rate)))
        bits, color_mode = c_int(), c_int()
        check("get color depth", get_depth(handle, ctypes.byref(bits), ctypes.byref(color_mode)))
        aoi = Rect()
        check("get 2x2 AOI", get_aoi(handle, IS_AOI_IMAGE_GET_AOI, ctypes.byref(aoi), ctypes.sizeof(aoi)))
        if bits.value != 32 or color_mode.value != 0:
            raise RuntimeError(f"unvalidated pixel format: {bits.value} bpp, color mode {color_mode.value}")
        print(f"Live format {aoi.s32Width}x{aoi.s32Height}, 2x2, 50 MHz, exposure {actual_exposure:.3f} ms, "
              f"driver rate {applied_rate.value:.2f} FPS")

        for _ in range(BUFFER_COUNT):
            mem, mem_id = c_void_p(), c_int()
            check("allocate image buffer", alloc(handle, aoi.s32Width, aoi.s32Height,
                                                  bits.value, ctypes.byref(mem), ctypes.byref(mem_id)))
            buffers.append((mem, mem_id))
            check("add sequence buffer", add_sequence(handle, mem, mem_id.value))
            sequence_ready = True
        width, height, depth, pitch = c_int(), c_int(), c_int(), c_int()
        check("inquire image memory", inquire(handle, buffers[0][0], buffers[0][1].value,
                                              ctypes.byref(width), ctypes.byref(height),
                                              ctypes.byref(depth), ctypes.byref(pitch)))
        if (width.value, height.value) != (aoi.s32Width, aoi.s32Height) or pitch.value < width.value * 4:
            raise RuntimeError(f"buffer dimensions/pitch mismatch: {width.value}x{height.value}, pitch={pitch.value}")

        mapping = {mem.value: mem_id.value for mem, mem_id in buffers}
        check("start continuous capture", capture_video(handle, IS_DONT_WAIT))
        live_started = True
        preview_title = "SC30 beads - ROI / bead selection"
        cv2.namedWindow(preview_title, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(preview_title, width.value, height.value)

        # Wait for one completed buffer; only the most recently completed frame is copied.
        first_frame = None
        previous_ptr = None
        wait_deadline = time.perf_counter() + 10
        while first_frame is None and time.perf_counter() < wait_deadline:
            active_id, active_ptr, last_ptr = c_int(), c_void_p(), c_void_p()
            rc = get_active(handle, ctypes.byref(active_id), ctypes.byref(active_ptr), ctypes.byref(last_ptr))
            ptr = last_ptr.value
            if rc == IS_SUCCESS and ptr and ptr in mapping and lock_buffer(handle, mapping[ptr], c_void_p(ptr)) == IS_SUCCESS:
                payload = ctypes.string_at(ptr, pitch.value * height.value)
                check("unlock first buffer", unlock_buffer(handle, mapping[ptr], c_void_p(ptr)))
                bgra = np.frombuffer(payload, np.uint8).reshape(height.value, pitch.value // 4, 4)
                first_frame = cv2.cvtColor(bgra[:, :width.value], cv2.COLOR_BGRA2BGR)
                previous_ptr = ptr
            else:
                cv2.waitKey(1)
                time.sleep(0.001)
        if first_frame is None:
            raise RuntimeError("No IDS sequence frame arrived within 10 seconds")

        print("Select the bead area (ROI) in the first image, then press Enter or Space.")
        roi = tuple(map(int, cv2.selectROI("Select bead ROI", first_frame,
                                           showCrosshair=True, fromCenter=False)))
        cv2.destroyWindow("Select bead ROI")
        if roi[2] <= 0 or roi[3] <= 0:
            print("ROI selection cancelled; no movie recorded.")
            return 0
        detector = create_detector()
        seeds_frame = first_frame.copy()
        candidates = detect_candidates(seeds_frame, roi, detector, args.min_size, args.max_size)
        print(f"Seed frame candidates in ROI: {len(candidates)}")
        seeds = choose_seeds(seeds_frame, roi, candidates)
        if not seeds:
            print("No beads selected; no movie recorded.")
            return 0

        tracks = {i: {"position": pos, "missed": 0, "size": 0.0}
                  for i, pos in enumerate(seeds, 1)}
        writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"mp4v"),
                                 max(1.0, applied_rate.value), (width.value, height.value))
        if not writer.isOpened():
            raise RuntimeError(f"Could not create video file: {video_path}")
        csv_handle = open(csv_path, "x", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_handle)
        csv_writer.writerow(["frame", "elapsed_s", "track_id", "status", "x_px", "y_px", "size_px"])
        print("Tracking and recording started. Press Q/Esc in the live window to stop.")
        cv2.setWindowTitle(preview_title, "SC30 live bead tracking - Q/Esc to stop")
        frame_no = shown = 0
        start_time = time.perf_counter()
        previous_display = None
        intervals = []
        last_report = start_time
        try:
            while args.seconds == 0 or time.perf_counter() - start_time < args.seconds:
                active_id, active_ptr, last_ptr = c_int(), c_void_p(), c_void_p()
                rc = get_active(handle, ctypes.byref(active_id), ctypes.byref(active_ptr), ctypes.byref(last_ptr))
                ptr = last_ptr.value
                if rc == IS_SUCCESS and ptr and ptr != previous_ptr and ptr in mapping:
                    mem_id = mapping[ptr]
                    if lock_buffer(handle, mem_id, c_void_p(ptr)) == IS_SUCCESS:
                        payload = ctypes.string_at(ptr, pitch.value * height.value)
                        unlock_rc = unlock_buffer(handle, mem_id, c_void_p(ptr))
                        check("unlock sequence buffer", unlock_rc)
                        bgra = np.frombuffer(payload, np.uint8).reshape(height.value, pitch.value // 4, 4)
                        frame = cv2.cvtColor(bgra[:, :width.value], cv2.COLOR_BGRA2BGR)
                        previous_ptr = ptr
                        frame_no += 1
                        elapsed = time.perf_counter() - start_time
                        candidates = detect_candidates(frame, roi, detector, args.min_size, args.max_size)
                        visible, tracks = associate(tracks, candidates, args.match_distance)
                        now = time.perf_counter()
                        if previous_display is not None:
                            intervals.append(now - previous_display)
                        previous_display = now
                        shown += 1
                        displayed_fps = (shown - 1) / (now - start_time) if shown > 1 else 0.0
                        lost_count = len(tracks) - len(visible)
                        annotated = annotate(frame, roi, visible, tracks, frame_no, displayed_fps, lost_count)
                        writer.write(annotated)
                        for bead_id, state in tracks.items():
                            point = visible.get(bead_id)
                            status = "visible" if point is not None else "missing"
                            position = point.pt if point is not None else state["position"]
                            size = point.size if point is not None else state.get("size", 0.0)
                            csv_writer.writerow([frame_no, f"{elapsed:.6f}", bead_id, status,
                                                 f"{position[0]:.3f}", f"{position[1]:.3f}", f"{size:.3f}"])
                        cv2.imshow(preview_title, annotated)
                        key = cv2.waitKey(1) & 0xFF
                        if key in (27, ord("q"), ord("Q")):
                            break
                    else:
                        key = cv2.waitKey(1) & 0xFF
                        if key in (27, ord("q"), ord("Q")):
                            break
                else:
                    key = cv2.waitKey(1) & 0xFF
                    if key in (27, ord("q"), ord("Q")):
                        break
                    time.sleep(0.0005)
                try:
                    if cv2.getWindowProperty(preview_title, cv2.WND_PROP_VISIBLE) < 1:
                        break
                except cv2.error:
                    break
                now = time.perf_counter()
                if now - last_report >= 2.0:
                    recent = intervals[-120:]
                    rate = 1.0 / statistics.mean(recent) if recent else 0.0
                    print(f"processed={shown}, displayed_fps={rate:.2f}, tracked={len(visible)}/{len(tracks)}, "
                          f"missed={lost_count}", flush=True)
                    last_report = now
        finally:
            writer.release()
            csv_handle.close()
        elapsed = time.perf_counter() - start_time
        final_fps = shown / elapsed if elapsed else 0
        print(f"Finished: frames={shown}, elapsed={elapsed:.2f}s, loop/display FPS={final_fps:.2f}")
        print(f"Saved annotated movie: {video_path}")
        print(f"Saved track data: {csv_path}")
    except KeyboardInterrupt:
        print("\nStopped by Ctrl+C; cleaning up and restoring settings.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        result = 5
    finally:
        cv2.destroyAllWindows()
        stop_and_free()
        if settings_saved:
            try:
                check("restore binning", set_binning(handle, int(old_binning)))
                current_aoi = Rect()
                check("read AOI for restoration", get_aoi(handle, IS_AOI_IMAGE_GET_AOI,
                                                           ctypes.byref(current_aoi), ctypes.sizeof(current_aoi)))
                original = (old_aoi.s32X, old_aoi.s32Y, old_aoi.s32Width, old_aoi.s32Height)
                current = (current_aoi.s32X, current_aoi.s32Y, current_aoi.s32Width, current_aoi.s32Height)
                if current != original:
                    check("restore AOI", get_aoi(handle, IS_AOI_IMAGE_SET_AOI,
                                                 ctypes.byref(old_aoi), ctypes.sizeof(old_aoi)))
                clock_value = c_uint(int(old_clock))
                check("restore pixel clock", pixel_clock(handle, IS_PIXELCLOCK_CMD_SET,
                                                         ctypes.byref(clock_value), ctypes.sizeof(clock_value)))
                restored = ctypes.c_double()
                check("restore FPS", set_fps(handle, float(old_fps), ctypes.byref(restored)))
                exposure = ctypes.c_double(float(old_exposure))
                check("restore exposure", set_exposure(handle, IS_EXPOSURE_SET,
                                                       ctypes.byref(exposure), ctypes.sizeof(exposure)))
                print(f"Settings restored: binning=0x{set_binning(handle, IS_GET_BINNING):04X}, "
                      f"pixel_clock={read_clock()} MHz, exposure={read_exposure():.3f} ms, "
                      f"FPS={restored.value:.3f}")
            except Exception as exc:
                print(f"WARNING restoring camera settings: {exc}", file=sys.stderr)
                result = 6
        print(f"is_ExitCamera: {exit_camera(handle)}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
