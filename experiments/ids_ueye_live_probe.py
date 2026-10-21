r"""Short, read-only-settings IDS uEye live-acquisition experiment for the Olympus SC30.

This is intentionally separate from the Olympus/CamAdm capture path.  It uses only
the installed uc480_64.dll API, reads the current AOI/color depth, allocates host
buffers, and starts/stops freerun acquisition.  It does not set exposure, gain,
frame rate, binning, AOI, or color mode.

Run from the repository root after closing DP2:
    .\.venv\Scripts\python.exe experiments\ids_ueye_live_probe.py --seconds 8
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
BUFFER_COUNT = 4


class CameraInfo(ctypes.Structure):
    _fields_ = [
        ("dwCameraID", c_uint32),
        ("dwDeviceID", c_uint32),
        ("dwSensorID", c_uint32),
        ("dwInUse", c_uint32),
        ("SerNo", c_char * 16),
        ("Model", c_char * 16),
        ("dwStatus", c_uint32),
        ("dwReserved", c_uint32 * 2),
        ("FullModelName", c_char * 32),
        ("dwReserved2", c_uint32 * 5),
    ]


class CameraListHead(ctypes.Structure):
    _fields_ = [("dwCount", c_uint32)]


class Rect(ctypes.Structure):
    _fields_ = [
        ("s32X", c_int32),
        ("s32Y", c_int32),
        ("s32Width", c_int32),
        ("s32Height", c_int32),
    ]


def decode(value: bytes) -> str:
    return value.split(b"\0", 1)[0].decode("ascii", "replace")


def bind(dll, name: str, argtypes: list[object]):
    fn = getattr(dll, name)
    fn.argtypes = argtypes
    fn.restype = c_int
    return fn


def fail_api(name: str, code: int) -> None:
    raise RuntimeError(f"{name} returned IDS error {code}")


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * q)))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--buffers", type=int, default=BUFFER_COUNT)
    parser.add_argument("--serial", default=TARGET_SERIAL)
    args = parser.parse_args()
    if args.seconds <= 0 or not 2 <= args.buffers <= 16:
        parser.error("--seconds must be positive and --buffers must be between 2 and 16")

    try:
        dll = ctypes.WinDLL(DLL_PATH)
    except OSError as exc:
        print(f"ERROR: cannot load {DLL_PATH}: {exc}", file=sys.stderr)
        return 2

    get_count = bind(dll, "is_GetNumberOfCameras", [ctypes.POINTER(c_int)])
    get_list = bind(dll, "is_GetCameraList", [c_void_p])
    count = c_int(-1)
    rc = get_count(ctypes.byref(count))
    if rc != IS_SUCCESS:
        print(f"ERROR: is_GetNumberOfCameras returned {rc}", file=sys.stderr)
        return 2
    if count.value <= 0:
        print("ERROR: IDS API reports no connected cameras", file=sys.stderr)
        return 2

    raw_list = ctypes.create_string_buffer(4 + count.value * ctypes.sizeof(CameraInfo))
    list_head = ctypes.cast(raw_list, ctypes.POINTER(CameraListHead)).contents
    list_head.dwCount = count.value
    rc = get_list(ctypes.byref(raw_list))
    if rc != IS_SUCCESS:
        print(f"ERROR: is_GetCameraList returned {rc}", file=sys.stderr)
        return 2
    infos = ctypes.cast(
        ctypes.addressof(raw_list) + ctypes.sizeof(c_uint32),
        ctypes.POINTER(CameraInfo),
    )
    camera = None
    for i in range(min(list_head.dwCount, count.value)):
        entry = infos[i]
        if decode(entry.SerNo) == args.serial:
            camera = entry
            break
    if camera is None:
        print(f"ERROR: SC30 serial {args.serial} is not in the IDS camera list", file=sys.stderr)
        return 3
    if camera.dwInUse:
        print(f"ERROR: camera serial {args.serial} is already in use; close DP2 and retry", file=sys.stderr)
        return 3

    init = bind(dll, "is_InitCamera", [ctypes.POINTER(c_int), c_void_p])
    exit_camera = bind(dll, "is_ExitCamera", [c_int])
    get_color_depth = bind(dll, "is_GetColorDepth", [c_int, ctypes.POINTER(c_int), ctypes.POINTER(c_int)])
    get_aoi = bind(dll, "is_AOI", [c_int, c_uint, c_void_p, c_uint])
    alloc = bind(dll, "is_AllocImageMem", [c_int, c_int, c_int, c_int, ctypes.POINTER(c_void_p), ctypes.POINTER(c_int)])
    add_sequence = bind(dll, "is_AddToSequence", [c_int, c_void_p, c_int])
    clear_sequence = bind(dll, "is_ClearSequence", [c_int])
    free_mem = bind(dll, "is_FreeImageMem", [c_int, c_void_p, c_int])
    capture_video = bind(dll, "is_CaptureVideo", [c_int, c_int])
    stop_video = bind(dll, "is_StopLiveVideo", [c_int, c_int])
    get_fps = bind(dll, "is_GetFramesPerSecond", [c_int, ctypes.POINTER(ctypes.c_double)])
    get_active = bind(dll, "is_GetActSeqBuf", [c_int, ctypes.POINTER(c_int), ctypes.POINTER(c_void_p), ctypes.POINTER(c_void_p)])
    lock_buffer = bind(dll, "is_LockSeqBuf", [c_int, c_int, c_void_p])
    unlock_buffer = bind(dll, "is_UnlockSeqBuf", [c_int, c_int, c_void_p])
    inquire_mem = bind(dll, "is_InquireImageMem", [c_int, c_void_p, c_int, ctypes.POINTER(c_int), ctypes.POINTER(c_int), ctypes.POINTER(c_int), ctypes.POINTER(c_int)])

    handle = c_int(int(camera.dwCameraID))
    rc = init(ctypes.byref(handle), None)
    if rc != IS_SUCCESS:
        print(f"ERROR: is_InitCamera returned {rc}; no acquisition was started", file=sys.stderr)
        return 4

    buffers: list[tuple[c_void_p, c_int, int, int, int]] = []
    live_started = False
    try:
        bits, color_mode = c_int(), c_int()
        rc = get_color_depth(handle, ctypes.byref(bits), ctypes.byref(color_mode))
        if rc != IS_SUCCESS:
            fail_api("is_GetColorDepth", rc)
        rect = Rect()
        rc = get_aoi(handle, IS_AOI_IMAGE_GET_AOI, ctypes.byref(rect), ctypes.sizeof(rect))
        if rc != IS_SUCCESS:
            fail_api("is_AOI(GET_AOI)", rc)
        if rect.s32Width <= 0 or rect.s32Height <= 0 or bits.value <= 0:
            raise RuntimeError(f"invalid current format: AOI={rect.s32Width}x{rect.s32Height}, bits={bits.value}")

        print(
            f"SC30 opened through uEye API: serial={args.serial}, handle={handle.value}; "
            f"current AOI={rect.s32Width}x{rect.s32Height}, color_depth={bits.value}, color_mode={color_mode.value}"
        )
        print("No exposure, gain, frame-rate, binning, AOI, or color-mode setters are called.")

        for _ in range(args.buffers):
            mem, mem_id = c_void_p(), c_int()
            rc = alloc(handle, rect.s32Width, rect.s32Height, bits.value, ctypes.byref(mem), ctypes.byref(mem_id))
            if rc != IS_SUCCESS:
                fail_api("is_AllocImageMem", rc)
            buffers.append((mem, mem_id, 0, 0, 0))
            rc = add_sequence(handle, mem, mem_id.value)
            if rc != IS_SUCCESS:
                fail_api("is_AddToSequence", rc)

        pitch = c_int()
        first_mem, first_id = buffers[0][0], buffers[0][1]
        x, y, depth = c_int(), c_int(), c_int()
        rc = inquire_mem(handle, first_mem, first_id, ctypes.byref(x), ctypes.byref(y), ctypes.byref(depth), ctypes.byref(pitch))
        if rc != IS_SUCCESS:
            fail_api("is_InquireImageMem", rc)
        frame_bytes = pitch.value * y.value
        if frame_bytes <= 0:
            raise RuntimeError(f"invalid image pitch/height: pitch={pitch.value}, height={y.value}")
        pointer_to_id = {mem.value: mem_id.value for mem, mem_id, *_ in buffers}
        print(f"host buffers={args.buffers}, frame={x.value}x{y.value} pitch={pitch.value} bytes={frame_bytes}")

        rc = capture_video(handle, IS_DONT_WAIT)
        if rc != IS_SUCCESS:
            fail_api("is_CaptureVideo(IS_DONT_WAIT)", rc)
        live_started = True

        observed: list[float] = []
        api_ms: list[float] = []
        copy_ms: list[float] = []
        frame_hashes: set[int] = set()
        frame_count = 0
        deadline = time.perf_counter() + args.seconds
        previous_ptr = None
        first_seen = last_seen = None
        driver_fps = ctypes.c_double(float("nan"))
        while time.perf_counter() < deadline:
            call_start = time.perf_counter()
            active_id, current_ptr, last_ptr = c_int(), c_void_p(), c_void_p()
            rc = get_active(handle, ctypes.byref(active_id), ctypes.byref(current_ptr), ctypes.byref(last_ptr))
            call_end = time.perf_counter()
            api_ms.append((call_end - call_start) * 1000.0)
            ptr_value = last_ptr.value
            if rc == IS_SUCCESS and ptr_value and ptr_value != previous_ptr:
                previous_ptr = ptr_value
                mem_id = pointer_to_id.get(ptr_value)
                if mem_id is None:
                    time.sleep(0.0005)
                    continue
                rc = lock_buffer(handle, mem_id, c_void_p(ptr_value))
                if rc == IS_SUCCESS:
                    copy_start = time.perf_counter()
                    payload = ctypes.string_at(ptr_value, frame_bytes)
                    copy_end = time.perf_counter()
                    unlock_rc = unlock_buffer(handle, mem_id, c_void_p(ptr_value))
                    if unlock_rc != IS_SUCCESS:
                        fail_api("is_UnlockSeqBuf", unlock_rc)
                    copy_ms.append((copy_end - copy_start) * 1000.0)
                    stamp = copy_end
                    observed.append(stamp)
                    frame_count += 1
                    frame_hashes.add(hash(payload))
                    first_seen = stamp if first_seen is None else first_seen
                    last_seen = stamp
            get_fps(handle, ctypes.byref(driver_fps))
            time.sleep(0.0005)

        if frame_count >= 2 and first_seen is not None and last_seen is not None and last_seen > first_seen:
            elapsed = last_seen - first_seen
            fps = (frame_count - 1) / elapsed
        else:
            elapsed = max(0.0, (last_seen or time.perf_counter()) - (first_seen or time.perf_counter()))
            fps = 0.0
        intervals_ms = [(b - a) * 1000.0 for a, b in zip(observed, observed[1:])]
        print(f"continuous capture complete: received={frame_count} observed_fps={fps:.2f} unique_frame_hashes={len(frame_hashes)}")
        print(f"driver-reported live FPS: {driver_fps.value:.2f}")
        print(f"frame interval ms: median={statistics.median(intervals_ms) if intervals_ms else float('nan'):.2f}, p95={percentile(intervals_ms, .95):.2f}")
        print(f"latest-buffer query ms: median={statistics.median(api_ms) if api_ms else float('nan'):.3f}, p95={percentile(api_ms, .95):.3f}")
        print(f"locked frame copy ms: median={statistics.median(copy_ms) if copy_ms else float('nan'):.3f}, p95={percentile(copy_ms, .95):.3f}")
        print("Timing is host-side frame-ready observation/copy timing; it is not motion-to-display latency.")
        return 0 if frame_count > 0 else 5
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 5
    finally:
        if live_started:
            stop_rc = stop_video(handle, IS_WAIT)
            print(f"is_StopLiveVideo(IS_WAIT): {stop_rc}")
        # is_AddToSequence may have succeeded for a prefix before an error.
        clear_sequence(handle)
        for mem, mem_id, *_ in buffers:
            free_rc = free_mem(handle, mem, mem_id.value)
            if free_rc != IS_SUCCESS:
                print(f"WARNING: is_FreeImageMem(id={mem_id.value}) returned {free_rc}", file=sys.stderr)
        exit_rc = exit_camera(handle)
        print(f"is_ExitCamera: {exit_rc}")


if __name__ == "__main__":
    raise SystemExit(main())
