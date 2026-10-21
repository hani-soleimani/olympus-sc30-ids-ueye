"""Reversible IDS uEye 2x2-binning/rate experiment for the Olympus SC30.

This is separate from the Olympus/CamAdm path. It opens the installed IDS
compatible driver directly, requests 2x2 binning, 10 ms exposure, and the
shortest driver-reported frame time, then measures delivered frame intervals
and verifies the allocated image buffers. Original binning, exposure, and
driver-reported frame rate are restored in ``finally``.

Run from the repository root after closing DP2 and any other camera client:
    .\\.venv\\Scripts\\python.exe experiments\\ids_ueye_binning2_rate_test.py --seconds 8
"""

from __future__ import annotations

import argparse
import ctypes
import statistics
import sys
import time
from ctypes import c_char, c_int, c_int32, c_uint, c_uint32, c_void_p


DLL_PATH = r"C:\Windows\System32\uc480_64.dll"
TARGET_SERIAL = "0057004412"
IS_SUCCESS = 0
IS_DONT_WAIT = 0
IS_WAIT = 1
IS_AOI_IMAGE_GET_AOI = 2
IS_BINNING_DISABLE = 0
IS_BINNING_2X_VERTICAL = 0x0001
IS_BINNING_2X_HORIZONTAL = 0x0002
IS_GET_BINNING = 0x8000
IS_GET_SUPPORTED_BINNING = 0x8001
IS_EXPOSURE_GET = 7
IS_EXPOSURE_SET = 12
BUFFER_COUNT = 6


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
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--exposure-ms", type=float, default=10.0)
    parser.add_argument("--serial", default=TARGET_SERIAL)
    args = parser.parse_args()
    if args.seconds < 2 or args.exposure_ms <= 0:
        parser.error("--seconds must be at least 2 and --exposure-ms must be positive")

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
    old_exposure = None
    old_fps = None
    old_fps_source = ""

    def read_exposure() -> float:
        value = ctypes.c_double()
        check("is_Exposure(GET_EXPOSURE)", set_exposure(handle, IS_EXPOSURE_GET, ctypes.byref(value), ctypes.sizeof(value)))
        if value.value <= 0:
            raise RuntimeError(f"invalid exposure readback: {value.value}")
        return value.value

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
        # Save all settings before the first write. The supported-binning mask
        # is checked before requesting the camera change.
        old_binning = set_binning(handle, IS_GET_BINNING)
        supported = set_binning(handle, IS_GET_SUPPORTED_BINNING)
        if old_binning < 0 or supported < 0:
            raise RuntimeError(f"binning query failed: current={old_binning}, supported-mask={supported}")
        requested_binning = IS_BINNING_2X_VERTICAL | IS_BINNING_2X_HORIZONTAL
        if supported & requested_binning != requested_binning:
            raise RuntimeError(f"2x2 binning is not reported as supported (mask=0x{supported:04X}); no settings changed")
        old_exposure = read_exposure()

        # Allocate a baseline sequence first, then measure the current driver
        # FPS without changing any camera setting.
        baseline_bits, baseline_color = c_int(), c_int()
        check("is_GetColorDepth(pre-test)", get_color_depth(handle, ctypes.byref(baseline_bits), ctypes.byref(baseline_color)))
        baseline_rect = Rect()
        check("is_AOI(GET_AOI, pre-test)", get_aoi(handle, IS_AOI_IMAGE_GET_AOI, ctypes.byref(baseline_rect), ctypes.sizeof(baseline_rect)))
        for _ in range(BUFFER_COUNT):
            mem, mem_id = c_void_p(), c_int()
            check("is_AllocImageMem(pre-test)", alloc(handle, baseline_rect.s32Width, baseline_rect.s32Height, baseline_bits.value, ctypes.byref(mem), ctypes.byref(mem_id)))
            buffers.append((mem, mem_id))
            check("is_AddToSequence(pre-test)", add_sequence(handle, mem, mem_id.value))
            sequence_ready = True
        check("is_CaptureVideo(pre-test)", capture_video(handle, IS_DONT_WAIT))
        live_started = True
        fps_readback = ctypes.c_double()
        for _ in range(10):
            rc = get_fps(handle, ctypes.byref(fps_readback))
            if rc == IS_SUCCESS and fps_readback.value > 0:
                break
            time.sleep(0.05)
        if rc != IS_SUCCESS or fps_readback.value <= 0:
            check("is_StopLiveVideo(pre-test)", stop_video(handle, IS_WAIT))
            live_started = False
            raise RuntimeError("could not read the current driver FPS; refusing to change settings without a restorable baseline")
        old_fps = fps_readback.value
        old_fps_source = "is_GetFramesPerSecond before changes"
        check("is_StopLiveVideo(pre-test)", stop_video(handle, IS_WAIT))
        live_started = False
        stop_and_release_buffers()
        settings_saved = True
        print(f"Original settings: binning=0x{old_binning:04X}, exposure={old_exposure:.6f} ms, frame-rate={old_fps:.6f} FPS ({old_fps_source})")
        print(f"Supported binning mask=0x{supported:04X}; request=2x2 (0x{requested_binning:04X})")

        check("is_SetBinning(2x2)", set_binning(handle, requested_binning))
        applied_binning = set_binning(handle, IS_GET_BINNING)
        if applied_binning != requested_binning:
            raise RuntimeError(f"2x2 binning readback mismatch: requested=0x{requested_binning:04X}, applied=0x{applied_binning:04X}")

        requested_exposure = ctypes.c_double(args.exposure_ms)
        check("is_Exposure(SET_EXPOSURE)", set_exposure(handle, IS_EXPOSURE_SET, ctypes.byref(requested_exposure), ctypes.sizeof(requested_exposure)))
        actual_exposure = read_exposure()

        # Query current timing limits after binning and exposure; ask the driver
        # for the fastest representable freerun frame rate.
        min_t, max_t, step_t = ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
        check("is_GetFrameTimeRange(2x2)", get_time_range(handle, ctypes.byref(min_t), ctypes.byref(max_t), ctypes.byref(step_t)))
        if min_t.value <= 0 or max_t.value <= 0:
            raise RuntimeError(f"invalid frame-time range: min={min_t.value}, max={max_t.value}")
        requested_fps = 1.0 / min_t.value
        applied_fps = ctypes.c_double()
        check("is_SetFrameRate(maximum)", set_fps(handle, requested_fps, ctypes.byref(applied_fps)))
        print(f"2x2 applied=0x{applied_binning:04X}; requested exposure={args.exposure_ms:.6f} ms, readback={actual_exposure:.6f} ms")
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

        pointer_to_id = {mem.value: mem_id.value for mem, mem_id in buffers}
        check("is_CaptureVideo(test)", capture_video(handle, IS_DONT_WAIT))
        live_started = True
        observed: list[float] = []
        query_ms: list[float] = []
        copy_ms: list[float] = []
        frame_hashes: set[int] = set()
        previous_ptr = None
        deadline = time.perf_counter() + args.seconds
        latest_driver_fps = ctypes.c_double(float("nan"))
        while time.perf_counter() < deadline:
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
                    observed.append(copied)
                    copy_ms.append((copied - copy_start) * 1000.0)
                    frame_hashes.add(hash(payload))
            get_fps(handle, ctypes.byref(latest_driver_fps))
            time.sleep(0.0005)
        intervals = [(b-a)*1000 for a, b in zip(observed, observed[1:])]
        elapsed = observed[-1] - observed[0] if len(observed) > 1 else 0.0
        measured_fps = (len(observed)-1)/elapsed if elapsed > 0 else 0.0
        print(f"Measured stream: frames={len(observed)}, elapsed={elapsed:.3f} s, delivered_fps={measured_fps:.2f}, driver_fps={latest_driver_fps.value:.2f}, unique_hashes={len(frame_hashes)}")
        print(f"Frame interval ms: mean={statistics.mean(intervals) if intervals else float('nan'):.3f}, median={statistics.median(intervals) if intervals else float('nan'):.3f}, p95={percentile(intervals, .95):.3f}, max={max(intervals) if intervals else float('nan'):.3f}")
        print(f"Host latest-buffer query ms: median={statistics.median(query_ms):.3f}, p95={percentile(query_ms, .95):.3f}; locked copy ms: median={statistics.median(copy_ms) if copy_ms else float('nan'):.3f}, p95={percentile(copy_ms, .95):.3f}")
        print("Delivered FPS is based on observed changed sequence buffers; this is not motion-to-display latency.")
        if len(observed) < 2:
            result = 5
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        result = 5
    finally:
        # Stop and release all streaming memory before restoring settings.
        stop_and_release_buffers()
        if settings_saved:
            try:
                check("restore is_SetBinning", set_binning(handle, int(old_binning)))
                if old_fps and old_fps > 0:
                    restored = ctypes.c_double()
                    check("restore is_SetFrameRate", set_fps(handle, float(old_fps), ctypes.byref(restored)))
                    print(f"Restored frame rate: requested={old_fps:.6f}, driver-applied={restored.value:.6f} FPS")
                exposure = ctypes.c_double(float(old_exposure))
                check("restore is_Exposure", set_exposure(handle, IS_EXPOSURE_SET, ctypes.byref(exposure), ctypes.sizeof(exposure)))
                print(f"Restored settings: binning=0x{set_binning(handle, IS_GET_BINNING):04X}, exposure={read_exposure():.6f} ms")
            except Exception as exc:
                print(f"WARNING: settings restoration/readback failed: {exc}", file=sys.stderr)
                result = 6
        rc = exit_camera(handle)
        print(f"is_ExitCamera: {rc}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
