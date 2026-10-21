"""Separate interactive SC30 preview at 2x2, 50 MHz, and about 35.8 FPS.

This separate experiment uses continuous IDS acquisition with a short sequence
buffer ring. It requests 2x2 binning, 50 MHz pixel clock, 10 ms exposure, and
the fastest driver-reported freerun frame timing. It copies and displays only
the newest completed sequence buffer. Original binning, AOI, pixel clock,
exposure, and configured frame rate are restored when the window closes or
Ctrl+C is pressed.

Run after closing DP2 and any other camera client:
    .\\.venv\\Scripts\\python.exe experiments\\ids_ueye_live_preview_50mhz.py
Press Q or Esc in the preview window to stop.
"""

from __future__ import annotations

import argparse
import ctypes
from collections import deque
import statistics
import sys
import time
from ctypes import c_char, c_int, c_int32, c_uint, c_uint32, c_void_p

try:
    import cv2
    import numpy as np
except ImportError as exc:
    raise SystemExit(f"OpenCV and NumPy are required for preview: {exc}")


DLL_PATH = r"C:\Windows\System32\uc480_64.dll"
TARGET_SERIAL = "0057004412"
IS_SUCCESS = 0
IS_DONT_WAIT = 0
IS_WAIT = 1
IS_AOI_IMAGE_GET_AOI = 2
IS_AOI_IMAGE_SET_AOI = 1
IS_BINNING_DISABLE = 0
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


def decode(value: bytes) -> str:
    return value.split(b"\0", 1)[0].decode("ascii", "replace")


def bind(dll, name: str, argtypes: list[object], restype=c_int):
    fn = getattr(dll, name)
    fn.argtypes = argtypes
    fn.restype = restype
    return fn


def check(name: str, code: int) -> None:
    if code != IS_SUCCESS:
        raise RuntimeError(f"{name} returned IDS error {code}")


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * q)))] if ordered else float("nan")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=0.0, help="optional run duration; default is until Q/Esc")
    parser.add_argument("--exposure-ms", type=float, default=10.0)
    parser.add_argument("--serial", default=TARGET_SERIAL)
    args = parser.parse_args()
    if args.seconds < 0 or (0 < args.seconds < 2) or args.exposure_ms <= 0:
        parser.error("--seconds must be 0 (until Q/Esc) or at least 2; --exposure-ms must be positive")

    try:
        dll = ctypes.WinDLL(DLL_PATH)
    except OSError as exc:
        print(f"ERROR: cannot load {DLL_PATH}: {exc}", file=sys.stderr)
        return 2

    get_count = bind(dll, "is_GetNumberOfCameras", [ctypes.POINTER(c_int)])
    get_list = bind(dll, "is_GetCameraList", [c_void_p])
    count = c_int()
    check("is_GetNumberOfCameras", get_count(ctypes.byref(count)))
    if count.value <= 0:
        print("ERROR: IDS API reports no connected cameras", file=sys.stderr)
        return 3
    raw_list = ctypes.create_string_buffer(4 + count.value * ctypes.sizeof(CameraInfo))
    ctypes.cast(raw_list, ctypes.POINTER(CameraListHead)).contents.dwCount = count.value
    check("is_GetCameraList", get_list(ctypes.byref(raw_list)))
    infos = ctypes.cast(ctypes.addressof(raw_list) + 4, ctypes.POINTER(CameraInfo))
    camera = next((infos[i] for i in range(count.value) if decode(infos[i].SerNo) == args.serial), None)
    if camera is None:
        print(f"ERROR: camera serial {args.serial} is not present", file=sys.stderr)
        return 3
    if camera.dwInUse:
        print(f"ERROR: camera {args.serial} is already in use; no settings changed", file=sys.stderr)
        return 3

    init = bind(dll, "is_InitCamera", [ctypes.POINTER(c_int), c_void_p])
    exit_camera = bind(dll, "is_ExitCamera", [c_int])
    set_binning = bind(dll, "is_SetBinning", [c_int, c_int])
    pixel_clock = bind(dll, "is_PixelClock", [c_int, c_uint, c_void_p, c_uint])
    set_exposure = bind(dll, "is_Exposure", [c_int, c_uint, c_void_p, c_uint], c_int)
    set_fps = bind(dll, "is_SetFrameRate", [c_int, ctypes.c_double, ctypes.POINTER(ctypes.c_double)])
    get_fps = bind(dll, "is_GetFramesPerSecond", [c_int, ctypes.POINTER(ctypes.c_double)])
    get_time_range = bind(dll, "is_GetFrameTimeRange", [c_int, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)])
    get_color_depth = bind(dll, "is_GetColorDepth", [c_int, ctypes.POINTER(c_int), ctypes.POINTER(c_int)])
    get_aoi = bind(dll, "is_AOI", [c_int, c_uint, c_void_p, c_uint])
    alloc = bind(dll, "is_AllocImageMem", [c_int, c_int, c_int, c_int, ctypes.POINTER(c_void_p), ctypes.POINTER(c_int)])
    add_sequence = bind(dll, "is_AddToSequence", [c_int, c_void_p, c_int])
    clear_sequence = bind(dll, "is_ClearSequence", [c_int])
    free_mem = bind(dll, "is_FreeImageMem", [c_int, c_void_p, c_int])
    capture_video = bind(dll, "is_CaptureVideo", [c_int, c_int])
    stop_video = bind(dll, "is_StopLiveVideo", [c_int, c_int])
    get_active = bind(dll, "is_GetActSeqBuf", [c_int, ctypes.POINTER(c_int), ctypes.POINTER(c_void_p), ctypes.POINTER(c_void_p)])
    lock_buffer = bind(dll, "is_LockSeqBuf", [c_int, c_int, c_void_p])
    unlock_buffer = bind(dll, "is_UnlockSeqBuf", [c_int, c_int, c_void_p])
    inquire_mem = bind(dll, "is_InquireImageMem", [c_int, c_void_p, c_int, ctypes.POINTER(c_int), ctypes.POINTER(c_int), ctypes.POINTER(c_int), ctypes.POINTER(c_int)])

    handle = c_int(int(camera.dwCameraID))
    check("is_InitCamera", init(ctypes.byref(handle), None))
    buffers: list[tuple[c_void_p, c_int]] = []
    live_started = False
    sequence_ready = False
    settings_saved = False
    old_binning = None
    old_aoi = None
    old_pixel_clock = None
    old_exposure = None
    old_fps = None
    old_fps_source = ""

    def read_exposure() -> float:
        value = ctypes.c_double()
        check("is_Exposure(GET_EXPOSURE)", set_exposure(handle, IS_EXPOSURE_GET, ctypes.byref(value), ctypes.sizeof(value)))
        if value.value <= 0:
            raise RuntimeError(f"invalid exposure readback: {value.value}")
        return value.value

    def read_pixel_clock() -> int:
        value = c_uint()
        check("is_PixelClock(GET)", pixel_clock(
            handle, IS_PIXELCLOCK_CMD_GET, ctypes.byref(value), ctypes.sizeof(value)))
        return value.value

    def set_pixel_clock(mhz: int) -> None:
        value = c_uint(mhz)
        check(f"is_PixelClock(SET {mhz} MHz)", pixel_clock(
            handle, IS_PIXELCLOCK_CMD_SET, ctypes.byref(value), ctypes.sizeof(value)))

    def stop_and_release_buffers() -> None:
        nonlocal live_started, sequence_ready
        if live_started:
            rc = stop_video(handle, IS_WAIT)
            print(f"restore: is_StopLiveVideo={rc}")
            live_started = False
        if sequence_ready:
            rc = clear_sequence(handle)
            print(f"restore: is_ClearSequence={rc}")
            sequence_ready = False
        for mem, mem_id in buffers:
            rc = free_mem(handle, mem, mem_id.value)
            if rc != IS_SUCCESS:
                print(f"WARNING: is_FreeImageMem returned {rc}")
        buffers.clear()

    result = 0
    try:
        # Save every setting this experiment can alter before the first write.
        old_binning = set_binning(handle, IS_GET_BINNING)
        supported = set_binning(handle, IS_GET_SUPPORTED_BINNING)
        if old_binning < 0 or supported < 0:
            raise RuntimeError(f"binning query failed: current={old_binning}, supported-mask={supported}")
        requested_binning = IS_BINNING_2X_VERTICAL | IS_BINNING_2X_HORIZONTAL
        if supported & requested_binning != requested_binning:
            raise RuntimeError(f"2x2 binning is not reported as supported (mask=0x{supported:04X}); no settings changed")
        old_aoi = Rect()
        check("is_AOI(GET_AOI, baseline)", get_aoi(handle, IS_AOI_IMAGE_GET_AOI, ctypes.byref(old_aoi), ctypes.sizeof(old_aoi)))
        old_pixel_clock = read_pixel_clock()
        old_exposure = read_exposure()
        fps_readback = ctypes.c_double()
        check("is_SetFrameRate(GET_FRAMERATE)", set_fps(handle, ctypes.c_double(IS_GET_FRAMERATE), ctypes.byref(fps_readback)))
        old_fps = fps_readback.value
        if old_fps <= 0:
            raise RuntimeError(f"invalid saved frame rate {old_fps}; refusing to change settings")
        old_fps_source = "IS_GET_FRAMERATE query"
        settings_saved = True
        print(f"Saved original settings: binning=0x{old_binning:04X}; AOI=({old_aoi.s32X},{old_aoi.s32Y},{old_aoi.s32Width},{old_aoi.s32Height}); pixel_clock={old_pixel_clock} MHz; exposure={old_exposure:.6f} ms; configured_fps={old_fps:.6f} ({old_fps_source})")
        print(f"Supported binning mask=0x{supported:04X}; requesting 2x2")

        check("is_SetBinning(2x2)", set_binning(handle, requested_binning))
        applied_binning = set_binning(handle, IS_GET_BINNING)
        if applied_binning != requested_binning:
            raise RuntimeError(f"2x2 binning readback mismatch: requested=0x{requested_binning:04X}, applied=0x{applied_binning:04X}")

        # Pixel-clock capabilities are mode dependent, so verify 50 MHz only
        # after binning has been applied. No preview starts if it is unavailable.
        clock_count = c_uint()
        check("is_PixelClock(GET_NUMBER)", pixel_clock(handle, IS_PIXELCLOCK_CMD_GET_NUMBER, ctypes.byref(clock_count), ctypes.sizeof(clock_count)))
        if not 0 < clock_count.value <= 150:
            raise RuntimeError(f"invalid pixel-clock count in 2x2 mode: {clock_count.value}")
        clocks = (c_uint * clock_count.value)()
        check("is_PixelClock(GET_LIST)", pixel_clock(handle, IS_PIXELCLOCK_CMD_GET_LIST, clocks, ctypes.sizeof(clocks)))
        supported_clocks = list(clocks)
        if 50 not in supported_clocks:
            raise RuntimeError(f"50 MHz is not supported in 2x2 mode; available clocks: {supported_clocks}")
        set_pixel_clock(50)
        applied_clock = read_pixel_clock()
        if applied_clock != 50:
            raise RuntimeError(f"pixel clock readback is {applied_clock} MHz, expected 50 MHz")
        print(f"2x2 pixel clocks supported: {supported_clocks}; applied={applied_clock} MHz")

        requested_exposure = ctypes.c_double(args.exposure_ms)
        check("is_Exposure(SET_EXPOSURE)", set_exposure(handle, IS_EXPOSURE_SET, ctypes.byref(requested_exposure), ctypes.sizeof(requested_exposure)))
        actual_exposure = read_exposure()

        # Query current timing limits after binning and exposure; ask the driver
        # for the fastest representable freerun frame rate.
        min_t, max_t, step_t = ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
        check("is_GetFrameTimeRange(2x2, 50 MHz)", get_time_range(handle, ctypes.byref(min_t), ctypes.byref(max_t), ctypes.byref(step_t)))
        if min_t.value <= 0 or max_t.value <= 0:
            raise RuntimeError(f"invalid frame-time range: min={min_t.value}, max={max_t.value}")
        requested_fps = 1.0 / min_t.value
        applied_fps = ctypes.c_double()
        check("is_SetFrameRate(maximum)", set_fps(handle, requested_fps, ctypes.byref(applied_fps)))
        print(f"Preview configuration: binning=2x2; pixel_clock={applied_clock} MHz; requested exposure={args.exposure_ms:.6f} ms, readback={actual_exposure:.6f} ms")
        print(f"Driver frame-time bounds={min_t.value*1000:.6f}..{max_t.value*1000:.6f} ms (step={step_t.value*1000:.6f} ms); requested max={requested_fps:.6f} FPS, applied={applied_fps.value:.6f} FPS")

        bits, color_mode = c_int(), c_int()
        check("is_GetColorDepth(2x2)", get_color_depth(handle, ctypes.byref(bits), ctypes.byref(color_mode)))
        rect = Rect()
        check("is_AOI(GET_AOI, 2x2)", get_aoi(handle, IS_AOI_IMAGE_GET_AOI, ctypes.byref(rect), ctypes.sizeof(rect)))
        if rect.s32Width <= 0 or rect.s32Height <= 0 or bits.value <= 0:
            raise RuntimeError(f"invalid post-binning format: AOI={rect.s32Width}x{rect.s32Height}, depth={bits.value}")

        for _ in range(BUFFER_COUNT):
            mem, mem_id = c_void_p(), c_int()
            check("is_AllocImageMem(2x2)", alloc(handle, rect.s32Width, rect.s32Height, bits.value, ctypes.byref(mem), ctypes.byref(mem_id)))
            buffers.append((mem, mem_id))
            check("is_AddToSequence(2x2)", add_sequence(handle, mem, mem_id.value))
            sequence_ready = True
        x, y, depth, pitch = c_int(), c_int(), c_int(), c_int()
        check("is_InquireImageMem(2x2)", inquire_mem(handle, buffers[0][0], buffers[0][1].value, ctypes.byref(x), ctypes.byref(y), ctypes.byref(depth), ctypes.byref(pitch)))
        frame_bytes = pitch.value * y.value
        if (x.value, y.value) != (rect.s32Width, rect.s32Height) or frame_bytes <= 0:
            raise RuntimeError(f"allocated buffer mismatch: AOI={rect.s32Width}x{rect.s32Height}, buffer={x.value}x{y.value}, pitch={pitch.value}")
        print(f"Image verified: AOI={rect.s32Width}x{rect.s32Height}; buffer={x.value}x{y.value}; depth={depth.value} bpp; color_mode={color_mode.value}; pitch={pitch.value} bytes; frame_bytes={frame_bytes}")
        if depth.value != 32 or color_mode.value != 0:
            raise RuntimeError(f"preview conversion is only validated for 32-bit RGB32 mode 0; got {depth.value} bpp, mode {color_mode.value}")

        pointer_to_id = {mem.value: mem_id.value for mem, mem_id in buffers}
        window = "Olympus SC30 IDS live - 2x2 50 MHz (Q/Esc to stop)"
        cv2.namedWindow(window, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window, x.value, y.value)
        check("is_CaptureVideo(preview)", capture_video(handle, IS_DONT_WAIT))
        live_started = True
        display_times: list[float] = []
        display_intervals: deque[float] = deque(maxlen=120)
        query_ms: list[float] = []
        copy_ms: list[float] = []
        previous_ptr = None
        start_time = time.perf_counter()
        deadline = start_time + args.seconds if args.seconds else None
        latest_driver_fps = ctypes.c_double(float("nan"))
        last_report = start_time
        shown = 0
        print("Live preview started. Move the bead and press Q or Esc in the image window to stop.", flush=True)
        while deadline is None or time.perf_counter() < deadline:
            start = time.perf_counter()
            active_id, current_ptr, last_ptr = c_int(), c_void_p(), c_void_p()
            rc = get_active(handle, ctypes.byref(active_id), ctypes.byref(current_ptr), ctypes.byref(last_ptr))
            end = time.perf_counter()
            query_ms.append((end - start) * 1000.0)
            ptr = last_ptr.value
            if rc == IS_SUCCESS and ptr and ptr != previous_ptr:
                previous_ptr = ptr
                mem_id = pointer_to_id.get(ptr)
                if mem_id is not None and lock_buffer(handle, mem_id, c_void_p(ptr)) == IS_SUCCESS:
                    copy_start = time.perf_counter()
                    payload = ctypes.string_at(ptr, frame_bytes)
                    copied = time.perf_counter()
                    check("is_UnlockSeqBuf", unlock_buffer(handle, mem_id, c_void_p(ptr)))
                    copy_ms.append((copied - copy_start) * 1000.0)
                    # uEye color mode 0 is legacy RGB32/BGRA8: 4 bytes per pixel.
                    bgra = np.frombuffer(payload, dtype=np.uint8).reshape(y.value, pitch.value // 4, 4)
                    bgr = cv2.cvtColor(bgra[:, :x.value, :], cv2.COLOR_BGRA2BGR)
                    recent = list(display_intervals)
                    recent_fps = 1000.0 / statistics.mean(recent) if recent else 0.0
                    lifetime_fps = ((shown - 1) / (display_times[-1] - display_times[0])) if len(display_times) > 1 else 0.0
                    interval_ms = recent[-1] if recent else 0.0
                    cv2.putText(bgr, f"Displayed: {lifetime_fps:.1f} FPS | Recent: {recent_fps:.1f} FPS | Driver: {latest_driver_fps.value:.1f} FPS",
                                (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 255, 0), 2, cv2.LINE_AA)
                    cv2.putText(bgr, f"2x2 | 50 MHz | {x.value}x{y.value} | displayed interval: {interval_ms:.1f} ms | Q/Esc stops",
                                (12, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (0, 255, 0), 2, cv2.LINE_AA)
                    cv2.imshow(window, bgr)
                    key = cv2.waitKey(1) & 0xFF
                    displayed = time.perf_counter()
                    if display_times:
                        display_intervals.append((displayed - display_times[-1]) * 1000.0)
                    display_times.append(displayed)
                    shown += 1
                    if key in (27, ord("q"), ord("Q")):
                        break
            else:
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q"), ord("Q")):
                    break
            try:
                if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                    break
            except cv2.error:
                break
            get_fps(handle, ctypes.byref(latest_driver_fps))
            now = time.perf_counter()
            if now - last_report >= 2.0:
                recent = list(display_intervals)
                rolling_fps = 1000.0 / statistics.mean(recent) if recent else 0.0
                print(f"preview: displayed={shown}, rolling_fps={rolling_fps:.2f}, driver_fps={latest_driver_fps.value:.2f}, interval_median_ms={statistics.median(recent) if recent else float('nan'):.2f}, interval_p95_ms={percentile(recent, .95):.2f}", flush=True)
                last_report = now
            if ptr == previous_ptr:
                time.sleep(0.0005)

        cv2.destroyWindow(window)
        intervals = list(display_intervals)
        elapsed = display_times[-1] - display_times[0] if len(display_times) > 1 else 0.0
        measured_fps = (shown - 1) / elapsed if elapsed > 0 else 0.0
        print(f"Preview ended: displayed={shown}, elapsed={elapsed:.3f} s, displayed_fps={measured_fps:.2f}, last_driver_fps={latest_driver_fps.value:.2f}")
        print(f"Displayed frame interval ms (last {len(intervals)}): median={statistics.median(intervals) if intervals else float('nan'):.3f}, p95={percentile(intervals, .95):.3f}, max={max(intervals) if intervals else float('nan'):.3f}")
        print(f"Host latest-buffer query ms: median={statistics.median(query_ms) if query_ms else float('nan'):.3f}, p95={percentile(query_ms, .95):.3f}; copy ms: median={statistics.median(copy_ms) if copy_ms else float('nan'):.3f}, p95={percentile(copy_ms, .95):.3f}")
        print("Displayed FPS measures host preview submissions; it is not motion-to-display latency.")
        if shown < 2:
            result = 5
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        result = 5
    finally:
        cv2.destroyAllWindows()
        # Stop and release all streaming memory before restoring settings.
        stop_and_release_buffers()
        if settings_saved:
            try:
                check("restore binning", set_binning(handle, int(old_binning)))
                current_aoi = Rect()
                check("read AOI before restore", get_aoi(handle, IS_AOI_IMAGE_GET_AOI, ctypes.byref(current_aoi), ctypes.sizeof(current_aoi)))
                original_aoi_values = (old_aoi.s32X, old_aoi.s32Y, old_aoi.s32Width, old_aoi.s32Height)
                current_aoi_values = (current_aoi.s32X, current_aoi.s32Y, current_aoi.s32Width, current_aoi.s32Height)
                if current_aoi_values != original_aoi_values:
                    check("restore AOI", get_aoi(handle, IS_AOI_IMAGE_SET_AOI, ctypes.byref(old_aoi), ctypes.sizeof(old_aoi)))
                set_pixel_clock(int(old_pixel_clock))
                if old_fps and old_fps > 0:
                    restored = ctypes.c_double()
                    check("restore is_SetFrameRate", set_fps(handle, float(old_fps), ctypes.byref(restored)))
                else:
                    restored = ctypes.c_double(float("nan"))
                exposure = ctypes.c_double(float(old_exposure))
                check("restore is_Exposure", set_exposure(handle, IS_EXPOSURE_SET, ctypes.byref(exposure), ctypes.sizeof(exposure)))
                final_aoi = Rect()
                check("verify restored AOI", get_aoi(handle, IS_AOI_IMAGE_GET_AOI, ctypes.byref(final_aoi), ctypes.sizeof(final_aoi)))
                final_clock = read_pixel_clock()
                final_exposure = read_exposure()
                final_binning = set_binning(handle, IS_GET_BINNING)
                final_fps = ctypes.c_double()
                check("verify restored frame rate", set_fps(handle, ctypes.c_double(IS_GET_FRAMERATE), ctypes.byref(final_fps)))
                print(f"Restored/read back: binning=0x{final_binning:04X}; AOI=({final_aoi.s32X},{final_aoi.s32Y},{final_aoi.s32Width},{final_aoi.s32Height}); pixel_clock={final_clock} MHz; exposure={final_exposure:.6f} ms; configured_fps={final_fps.value:.6f} (setter restore applied={restored.value:.6f})")
                final_aoi_values = (final_aoi.s32X, final_aoi.s32Y, final_aoi.s32Width, final_aoi.s32Height)
                if (final_binning != old_binning or final_aoi_values != original_aoi_values or
                    final_clock != old_pixel_clock or abs(final_exposure - old_exposure) > 0.1 or
                    abs(final_fps.value - old_fps) > 0.1):
                    raise RuntimeError("restored values differ from the saved baseline")
            except Exception as exc:
                print(f"WARNING: settings restoration/readback failed: {exc}", file=sys.stderr)
                result = 6
        rc = exit_camera(handle)
        print(f"is_ExitCamera: {rc}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
