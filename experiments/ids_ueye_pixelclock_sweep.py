"""Reversible 2x2 binning pixel-clock/FPS comparison for the SC30.

Tests continuous IDS acquisition at 24 MHz and at the highest pixel clock
reported after enabling 2x2 binning. Saves and restores the original AOI,
binning, clock, exposure, and configured frame rate. Does not edit or call the
separate live-preview script.

Run from the repository root with DP2 and the IDS preview closed:
    .\\.venv\\Scripts\\python.exe experiments\\ids_ueye_pixelclock_sweep.py --seconds 8
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
IS_AOI_IMAGE_SET_AOI = 1
IS_BINNING_2X_VERTICAL = 0x0001
IS_BINNING_2X_HORIZONTAL = 0x0002
IS_GET_BINNING = 0x8000
IS_GET_SUPPORTED_BINNING = 0x8001
IS_EXPOSURE_GET = 7
IS_EXPOSURE_SET = 12
IS_PIXELCLOCK_CMD_GET_NUMBER = 1
IS_PIXELCLOCK_CMD_GET_LIST = 2
IS_PIXELCLOCK_CMD_GET_RANGE = 3
IS_PIXELCLOCK_CMD_GET_DEFAULT = 4
IS_PIXELCLOCK_CMD_GET = 5
IS_PIXELCLOCK_CMD_SET = 6
IS_GET_FRAMERATE = 0x8000
BUFFER_COUNT = 8
POLL_SLEEP_S = 0.00025


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
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * q)))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=8.0,
                        help="measurement duration per pixel clock (default: 8)")
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
        print("ERROR: IDS API reports no cameras", file=sys.stderr)
        return 3
    raw = ctypes.create_string_buffer(4 + count.value * ctypes.sizeof(CameraInfo))
    ctypes.cast(raw, ctypes.POINTER(CameraListHead)).contents.dwCount = count.value
    check("is_GetCameraList", get_list(ctypes.byref(raw)))
    infos = ctypes.cast(ctypes.addressof(raw) + 4, ctypes.POINTER(CameraInfo))
    camera = next((infos[i] for i in range(count.value)
                   if decode(infos[i].SerNo) == args.serial), None)
    if camera is None:
        print(f"ERROR: camera {args.serial} not present", file=sys.stderr)
        return 3
    if camera.dwInUse:
        print(f"ERROR: camera {args.serial} is in use; no changes made. Close DP2/preview and retry.", file=sys.stderr)
        return 3

    init = bind(dll, "is_InitCamera", [ctypes.POINTER(c_int), c_void_p])
    exit_camera = bind(dll, "is_ExitCamera", [c_int])
    set_binning = bind(dll, "is_SetBinning", [c_int, c_int])
    aoi = bind(dll, "is_AOI", [c_int, c_uint, c_void_p, c_uint])
    pixel_clock = bind(dll, "is_PixelClock", [c_int, c_uint, c_void_p, c_uint])
    exposure_api = bind(dll, "is_Exposure", [c_int, c_uint, c_void_p, c_uint])
    set_fps = bind(dll, "is_SetFrameRate", [c_int, ctypes.c_double,
                                              ctypes.POINTER(ctypes.c_double)])
    get_fps = bind(dll, "is_GetFramesPerSecond", [c_int,
                                                    ctypes.POINTER(ctypes.c_double)])
    get_time_range = bind(dll, "is_GetFrameTimeRange", [c_int,
        ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double)])
    get_color_depth = bind(dll, "is_GetColorDepth", [c_int,
        ctypes.POINTER(c_int), ctypes.POINTER(c_int)])
    alloc = bind(dll, "is_AllocImageMem", [c_int, c_int, c_int, c_int,
        ctypes.POINTER(c_void_p), ctypes.POINTER(c_int)])
    add_sequence = bind(dll, "is_AddToSequence", [c_int, c_void_p, c_int])
    clear_sequence = bind(dll, "is_ClearSequence", [c_int])
    free_mem = bind(dll, "is_FreeImageMem", [c_int, c_void_p, c_int])
    capture_video = bind(dll, "is_CaptureVideo", [c_int, c_int])
    stop_video = bind(dll, "is_StopLiveVideo", [c_int, c_int])
    get_active = bind(dll, "is_GetActSeqBuf", [c_int, ctypes.POINTER(c_int),
        ctypes.POINTER(c_void_p), ctypes.POINTER(c_void_p)])
    inquire_mem = bind(dll, "is_InquireImageMem", [c_int, c_void_p, c_int,
        ctypes.POINTER(c_int), ctypes.POINTER(c_int), ctypes.POINTER(c_int),
        ctypes.POINTER(c_int)])

    handle = c_int(int(camera.dwCameraID))
    check("is_InitCamera", init(ctypes.byref(handle), None))
    saved = False
    streaming = False
    sequence_ready = False
    buffers: list[tuple[c_void_p, c_int]] = []
    result = 0
    old_binning = old_aoi = old_clock = old_exposure = old_fps = None

    def query_exposure() -> float:
        value = ctypes.c_double()
        check("is_Exposure(GET_EXPOSURE)", exposure_api(
            handle, IS_EXPOSURE_GET, ctypes.byref(value), ctypes.sizeof(value)))
        return value.value

    def set_clock(mhz: int) -> None:
        value = c_uint(mhz)
        check(f"is_PixelClock(SET {mhz} MHz)", pixel_clock(
            handle, IS_PIXELCLOCK_CMD_SET, ctypes.byref(value), ctypes.sizeof(value)))

    def query_clock() -> int:
        value = c_uint()
        check("is_PixelClock(GET)", pixel_clock(
            handle, IS_PIXELCLOCK_CMD_GET, ctypes.byref(value), ctypes.sizeof(value)))
        return value.value

    def query_pixel_clock_list() -> tuple[int, int, list[int]]:
        bounds = (c_uint * 3)()
        rc = pixel_clock(handle, IS_PIXELCLOCK_CMD_GET_RANGE, bounds, ctypes.sizeof(bounds))
        check("is_PixelClock(GET_RANGE)", rc)
        n = c_uint()
        check("is_PixelClock(GET_NUMBER)", pixel_clock(
            handle, IS_PIXELCLOCK_CMD_GET_NUMBER, ctypes.byref(n), ctypes.sizeof(n)))
        if not 0 < n.value <= 150:
            raise RuntimeError(f"invalid supported pixel-clock count: {n.value}")
        values = (c_uint * n.value)()
        check("is_PixelClock(GET_LIST)", pixel_clock(
            handle, IS_PIXELCLOCK_CMD_GET_LIST, values, ctypes.sizeof(values)))
        return int(bounds[0]), int(bounds[1]), list(values)

    def release_buffers() -> None:
        nonlocal sequence_ready
        if sequence_ready:
            rc = clear_sequence(handle)
            print(f"  is_ClearSequence: {rc}")
            sequence_ready = False
        for memory, memory_id in buffers:
            rc = free_mem(handle, memory, memory_id.value)
            if rc != IS_SUCCESS:
                print(f"  WARNING is_FreeImageMem returned {rc}")
        buffers.clear()

    def capture_test(clock_mhz: int, label: str) -> None:
        nonlocal streaming, sequence_ready
        set_clock(clock_mhz)
        applied_clock = query_clock()
        min_mhz, max_mhz, clocks = query_pixel_clock_list()

        # Clock changes affect both timing ranges; set exposure after the clock,
        # then query the new frame-time range before selecting the FPS request.
        requested_exposure = ctypes.c_double(args.exposure_ms)
        check("is_Exposure(SET_EXPOSURE)", exposure_api(
            handle, IS_EXPOSURE_SET, ctypes.byref(requested_exposure),
            ctypes.sizeof(requested_exposure)))
        actual_exposure = query_exposure()
        tmin, tmax, tinc = ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
        check("is_GetFrameTimeRange", get_time_range(
            handle, ctypes.byref(tmin), ctypes.byref(tmax), ctypes.byref(tinc)))
        if tmin.value <= 0 or tmax.value <= 0:
            raise RuntimeError(f"invalid frame-time bounds: {tmin.value}, {tmax.value}")
        requested_fps = 1.0 / tmin.value
        applied_fps = ctypes.c_double()
        check("is_SetFrameRate(maximum)", set_fps(
            handle, requested_fps, ctypes.byref(applied_fps)))
        # Read the limits again after the frame-rate request so report reflects
        # the final tested camera state, not just the pre-request estimate.
        check("is_GetFrameTimeRange(post-rate)", get_time_range(
            handle, ctypes.byref(tmin), ctypes.byref(tmax), ctypes.byref(tinc)))
        print(f"\n[{label}] Pixel clock requested={clock_mhz} MHz; applied/readback={applied_clock} MHz; mode-supported range={min_mhz}..{max_mhz} MHz; list={clocks}")
        print(f"  Exposure requested={args.exposure_ms:.6f} ms; applied={actual_exposure:.6f} ms")
        print(f"  Frame-time range after clock/exposure/rate: min={tmin.value*1000:.6f} ms ({1/tmin.value:.6f} FPS ceiling), max={tmax.value*1000:.6f} ms ({1/tmax.value:.6f} FPS floor), step={tinc.value*1000:.6f} ms")
        print(f"  Frame rate requested={requested_fps:.6f}; applied by driver={applied_fps.value:.6f} FPS")

        bits, color_mode = c_int(), c_int()
        check("is_GetColorDepth", get_color_depth(
            handle, ctypes.byref(bits), ctypes.byref(color_mode)))
        rect = Rect()
        check("is_AOI(GET_AOI)", aoi(
            handle, IS_AOI_IMAGE_GET_AOI, ctypes.byref(rect), ctypes.sizeof(rect)))
        if (rect.s32Width, rect.s32Height) != (1024, 768):
            raise RuntimeError(f"2x2 output is {rect.s32Width}x{rect.s32Height}, expected 1024x768")

        for _ in range(BUFFER_COUNT):
            memory, memory_id = c_void_p(), c_int()
            check("is_AllocImageMem", alloc(handle, rect.s32Width,
                rect.s32Height, bits.value, ctypes.byref(memory),
                ctypes.byref(memory_id)))
            buffers.append((memory, memory_id))
            check("is_AddToSequence", add_sequence(handle, memory, memory_id.value))
            sequence_ready = True
        width, height, depth, pitch = c_int(), c_int(), c_int(), c_int()
        check("is_InquireImageMem", inquire_mem(handle, buffers[0][0],
            buffers[0][1].value, ctypes.byref(width), ctypes.byref(height),
            ctypes.byref(depth), ctypes.byref(pitch)))
        if (width.value, height.value) != (1024, 768):
            raise RuntimeError(f"image buffer is {width.value}x{height.value}")
        print(f"  Image buffer verified: {width.value}x{height.value}, {depth.value} bpp, color_mode={color_mode.value}, pitch={pitch.value}, bytes={pitch.value * height.value}")

        ids_by_pointer = {memory.value: memory_id.value for memory, memory_id in buffers}
        check("is_CaptureVideo", capture_video(handle, IS_DONT_WAIT))
        streaming = True
        observed_times: list[float] = []
        observed_ids: list[int] = []
        query_errors = 0
        latest_fps = ctypes.c_double(float("nan"))
        deadline = time.perf_counter() + args.seconds
        while time.perf_counter() < deadline:
            active_id, current_ptr, last_ptr = c_int(), c_void_p(), c_void_p()
            rc = get_active(handle, ctypes.byref(active_id),
                            ctypes.byref(current_ptr), ctypes.byref(last_ptr))
            now = time.perf_counter()
            if rc != IS_SUCCESS:
                query_errors += 1
            else:
                ptr = last_ptr.value
                memory_id = ids_by_pointer.get(ptr)
                if memory_id is not None and (not observed_ids or memory_id != observed_ids[-1]):
                    observed_ids.append(memory_id)
                    observed_times.append(now)
            get_fps(handle, ctypes.byref(latest_fps))
            time.sleep(POLL_SLEEP_S)

        check("is_StopLiveVideo", stop_video(handle, IS_WAIT))
        streaming = False
        intervals = [(b - a) * 1000.0 for a, b in zip(observed_times, observed_times[1:])]
        elapsed = observed_times[-1] - observed_times[0] if len(observed_times) > 1 else 0.0
        delivered = (len(observed_times) - 1) / elapsed if elapsed > 0 else 0.0
        # Buffer-ID jumps estimate frames whose sequence slots were passed
        # between host polls. This is a host-observation gap count, not proof of
        # sensor/USB frame loss.
        gaps = 0
        for prev_id, next_id in zip(observed_ids, observed_ids[1:]):
            delta = (next_id - prev_id) % BUFFER_COUNT
            if delta > 1:
                gaps += delta - 1
        print(f"  Measurement: observed={len(observed_times)} frames; delivered_fps={delivered:.3f}; driver_live_fps={latest_fps.value:.3f}; elapsed={elapsed:.3f}s; GetActSeqBuf errors={query_errors}")
        print(f"  Intervals ms: mean={statistics.mean(intervals) if intervals else float('nan'):.3f}; median={statistics.median(intervals) if intervals else float('nan'):.3f}; p95={percentile(intervals, .95):.3f}; max={max(intervals) if intervals else float('nan'):.3f}")
        print(f"  Inferred skipped sequence slots between polls={gaps} (host observation gaps; does not by itself establish sensor/USB frame drops)")
        release_buffers()

    try:
        old_binning = set_binning(handle, IS_GET_BINNING)
        supported_binning = set_binning(handle, IS_GET_SUPPORTED_BINNING)
        old_aoi = Rect()
        check("is_AOI(GET_AOI, baseline)", aoi(
            handle, IS_AOI_IMAGE_GET_AOI, ctypes.byref(old_aoi), ctypes.sizeof(old_aoi)))
        old_clock = query_clock()
        old_exposure = query_exposure()
        old_fps_value = ctypes.c_double()
        check("is_SetFrameRate(GET_FRAMERATE)", set_fps(
            handle, ctypes.c_double(IS_GET_FRAMERATE), ctypes.byref(old_fps_value)))
        if old_binning < 0 or not old_fps_value.value > 0 or old_exposure <= 0:
            raise RuntimeError(f"could not save restorable baseline: binning={old_binning}, fps={old_fps_value.value}, exposure={old_exposure}")
        old_fps = old_fps_value.value
        saved = True
        print(f"Baseline saved: binning=0x{old_binning:04X} (supported=0x{supported_binning:04X}); AOI=({old_aoi.s32X},{old_aoi.s32Y},{old_aoi.s32Width},{old_aoi.s32Height}); pixel_clock={old_clock} MHz; exposure={old_exposure:.6f} ms; configured_fps={old_fps:.6f}")

        requested_binning = IS_BINNING_2X_VERTICAL | IS_BINNING_2X_HORIZONTAL
        if supported_binning & requested_binning != requested_binning:
            raise RuntimeError("2x2 binning is not supported; no experiment settings changed")
        check("is_SetBinning(2x2)", set_binning(handle, requested_binning))
        applied_binning = set_binning(handle, IS_GET_BINNING)
        if applied_binning != requested_binning:
            raise RuntimeError(f"2x2 binning readback mismatch: 0x{applied_binning:04X}")
        clock_min, clock_max, clocks = query_pixel_clock_list()
        if 24 not in clocks:
            raise RuntimeError(f"24 MHz is not supported in 2x2 mode; supported clocks are {clocks}")
        if not clocks:
            raise RuntimeError("driver returned an empty pixel-clock list")
        high_clock = max(clocks)
        print(f"2x2 mode active. Pixel-clock range={clock_min}..{clock_max} MHz; supported={clocks}; comparing 24 MHz with {high_clock} MHz maximum.")

        capture_test(24, "24 MHz")
        if high_clock != 24:
            capture_test(high_clock, f"highest supported ({high_clock} MHz)")
        else:
            print("Highest supported clock equals 24 MHz; second comparison is identical and was skipped.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        result = 5
    finally:
        if streaming:
            rc = stop_video(handle, IS_WAIT)
            print(f"cleanup: is_StopLiveVideo={rc}")
            streaming = False
        release_buffers()
        if saved:
            try:
                # Restore sensor geometry first, then clock and the dependent
                # acquisition settings. Restore AOI explicitly if binning did
                # not return it to the original rectangle.
                check("restore binning", set_binning(handle, int(old_binning)))
                current = Rect()
                check("read AOI before restore", aoi(
                    handle, IS_AOI_IMAGE_GET_AOI, ctypes.byref(current), ctypes.sizeof(current)))
                if (current.s32X, current.s32Y, current.s32Width, current.s32Height) != (
                    old_aoi.s32X, old_aoi.s32Y, old_aoi.s32Width, old_aoi.s32Height
                ):
                    check("restore AOI", aoi(handle, IS_AOI_IMAGE_SET_AOI,
                        ctypes.byref(old_aoi), ctypes.sizeof(old_aoi)))
                set_clock(int(old_clock))
                restored_fps = ctypes.c_double()
                check("restore frame rate", set_fps(handle, float(old_fps), ctypes.byref(restored_fps)))
                requested_exposure = ctypes.c_double(float(old_exposure))
                check("restore exposure", exposure_api(handle, IS_EXPOSURE_SET,
                    ctypes.byref(requested_exposure), ctypes.sizeof(requested_exposure)))
                final_aoi = Rect()
                check("verify restored AOI", aoi(handle, IS_AOI_IMAGE_GET_AOI,
                    ctypes.byref(final_aoi), ctypes.sizeof(final_aoi)))
                final_clock = query_clock()
                final_exposure = query_exposure()
                final_binning = set_binning(handle, IS_GET_BINNING)
                print(f"Restored/read back: binning=0x{final_binning:04X}; AOI=({final_aoi.s32X},{final_aoi.s32Y},{final_aoi.s32Width},{final_aoi.s32Height}); pixel_clock={final_clock} MHz; exposure={final_exposure:.6f} ms; frame_rate_setter_applied={restored_fps.value:.6f} FPS")
                if (final_binning != old_binning or
                    (final_aoi.s32X, final_aoi.s32Y, final_aoi.s32Width, final_aoi.s32Height) !=
                    (old_aoi.s32X, old_aoi.s32Y, old_aoi.s32Width, old_aoi.s32Height) or
                    final_clock != old_clock or
                    abs(final_exposure - old_exposure) > 0.1):
                    raise RuntimeError("restored settings did not match saved baseline")
            except Exception as exc:
                print(f"RESTORE ERROR: {exc}", file=sys.stderr)
                result = 6
        rc = exit_camera(handle)
        print(f"is_ExitCamera: {rc}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
