r"""Experimental IDS uEye BGR8 preview with fixed white-balance comparison.

Separate from the known-good preview. Configures 2x2 binning, 50 MHz,
approximately 10 ms exposure, and maximum driver-reported frame rate.
Explicitly requests legacy RGB24 (packed BGR8), validates mode/depth/pitch,
saves the first exact raw frame, displays it without pixel conversion, and
restores camera settings when stopped.

Run from the olympus-sc30-ids-ueye project root after closing DP2:
    .\.venv\Scripts\python.exe experiments\ids_ueye_color_balance_preview.py --calibrate-once
Press Q/Esc in the preview window to stop.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import sys
import time
from collections import deque
from ctypes import c_char, c_int, c_int32, c_uint, c_uint32, c_void_p
from datetime import datetime
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError as exc:
    raise SystemExit(f"OpenCV and NumPy are required: {exc}")

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
IS_GET_COLOR_MODE = 0x8000
IS_GET_BITS_PER_PIXEL = 0x8001
IS_SET_CM_RGB24 = 1
IS_GET_CCOR_MODE = 0x8000
IS_GET_SUPPORTED_CCOR_MODE = 0x8001
IS_GET_ENABLE_AUTO_WHITEBALANCE = 0x8805
IS_GET_MASTER_GAIN = 0x8000
IS_GET_RED_GAIN = 0x8001
IS_GET_GREEN_GAIN = 0x8002
IS_GET_BLUE_GAIN = 0x8003
IS_IGNORE_PARAMETER = -1
IS_EXPOSURE_GET = 7
IS_EXPOSURE_SET = 12
IS_PIXELCLOCK_CMD_GET_NUMBER = 1
IS_PIXELCLOCK_CMD_GET_LIST = 2
IS_PIXELCLOCK_CMD_GET = 5
IS_PIXELCLOCK_CMD_SET = 6
IS_GET_FRAMERATE = 0x8000
BUFFER_COUNT = 6
WINDOW = "Olympus SC30 BGR8 white-balance comparison (Q/Esc stops)"
ROI_WINDOW = "Select a neutral patch in the raw BGR image"


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
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="stop automatically after this many seconds; 0 means until Q/Esc")
    parser.add_argument("--calibrate-once", action="store_true",
                        help="select a neutral area once from the first frame; gains then stay fixed")
    parser.add_argument("--neutral-roi", type=str, default=None,
                        help="calibrate once from x,y,width,height in the first frame, e.g. 350,250,100,100")
    args = parser.parse_args()
    if args.seconds < 0 or (0 < args.seconds < 3):
        parser.error("--seconds must be 0 (until Q/Esc) or at least 3")
    if args.calibrate_once and args.neutral_roi:
        parser.error("choose either --calibrate-once or --neutral-roi")
    roi = None
    if args.neutral_roi:
        try:
            roi = tuple(int(v.strip()) for v in args.neutral_roi.split(","))
        except ValueError:
            parser.error("--neutral-roi must be x,y,width,height integers")
        if len(roi) != 4 or roi[2] <= 0 or roi[3] <= 0:
            parser.error("--neutral-roi must be x,y,width,height with positive width and height")
    try:
        dll = ctypes.WinDLL(DLL_PATH)
    except OSError as exc:
        print(f"ERROR loading IDS API {DLL_PATH}: {exc}", file=sys.stderr)
        return 2

    get_count = bind(dll, "is_GetNumberOfCameras", [ctypes.POINTER(c_int)])
    get_list = bind(dll, "is_GetCameraList", [c_void_p])
    count = c_int()
    check("is_GetNumberOfCameras", get_count(ctypes.byref(count)))
    if count.value < 1:
        print("ERROR: no IDS camera is enumerated", file=sys.stderr)
        return 3
    raw_list = ctypes.create_string_buffer(4 + count.value * ctypes.sizeof(CameraInfo))
    ctypes.cast(raw_list, ctypes.POINTER(CameraListHead)).contents.dwCount = count.value
    check("is_GetCameraList", get_list(ctypes.byref(raw_list)))
    infos = ctypes.cast(ctypes.addressof(raw_list) + 4, ctypes.POINTER(CameraInfo))
    camera = next((infos[i] for i in range(count.value)
                   if decode(infos[i].SerNo) == TARGET_SERIAL), None)
    if camera is None:
        print(f"ERROR: SC30 {TARGET_SERIAL} was not found", file=sys.stderr)
        return 3
    if camera.dwInUse:
        print("ERROR: the camera is already in use. Close DP2 and other camera clients first; no settings changed.",
              file=sys.stderr)
        return 3

    init = bind(dll, "is_InitCamera", [ctypes.POINTER(c_int), c_void_p])
    exit_camera = bind(dll, "is_ExitCamera", [c_int])
    set_color_mode = bind(dll, "is_SetColorMode", [c_int, c_int])
    hardware_gain = bind(dll, "is_SetHardwareGain", [c_int, c_int, c_int, c_int, c_int])
    color_correction = bind(dll, "is_SetColorCorrection", [c_int, c_int, ctypes.POINTER(ctypes.c_double)])
    auto_parameter = bind(dll, "is_SetAutoParameter", [c_int, c_int, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)])
    get_wb_multipliers = bind(dll, "is_GetWhiteBalanceMultipliers", [c_int, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)])
    set_binning = bind(dll, "is_SetBinning", [c_int, c_int])
    aoi = bind(dll, "is_AOI", [c_int, c_uint, c_void_p, c_uint])
    pixel_clock = bind(dll, "is_PixelClock", [c_int, c_uint, c_void_p, c_uint])
    exposure = bind(dll, "is_Exposure", [c_int, c_uint, c_void_p, c_uint])
    set_fps = bind(dll, "is_SetFrameRate", [c_int, ctypes.c_double,
        ctypes.POINTER(ctypes.c_double)])
    get_fps = bind(dll, "is_GetFramesPerSecond", [c_int, ctypes.POINTER(ctypes.c_double)])
    get_time_range = bind(dll, "is_GetFrameTimeRange", [c_int,
        ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double)])
    alloc = bind(dll, "is_AllocImageMem", [c_int, c_int, c_int, c_int,
        ctypes.POINTER(c_void_p), ctypes.POINTER(c_int)])
    add_sequence = bind(dll, "is_AddToSequence", [c_int, c_void_p, c_int])
    clear_sequence = bind(dll, "is_ClearSequence", [c_int])
    free_mem = bind(dll, "is_FreeImageMem", [c_int, c_void_p, c_int])
    capture = bind(dll, "is_CaptureVideo", [c_int, c_int])
    stop = bind(dll, "is_StopLiveVideo", [c_int, c_int])
    get_active = bind(dll, "is_GetActSeqBuf", [c_int, ctypes.POINTER(c_int),
        ctypes.POINTER(c_void_p), ctypes.POINTER(c_void_p)])
    lock = bind(dll, "is_LockSeqBuf", [c_int, c_int, c_void_p])
    unlock = bind(dll, "is_UnlockSeqBuf", [c_int, c_int, c_void_p])
    inquire = bind(dll, "is_InquireImageMem", [c_int, c_void_p, c_int,
        ctypes.POINTER(c_int), ctypes.POINTER(c_int), ctypes.POINTER(c_int),
        ctypes.POINTER(c_int)])

    handle = c_int(int(camera.dwCameraID))
    check("is_InitCamera", init(ctypes.byref(handle), None))
    initialized = True
    buffers: list[tuple[c_void_p, c_int]] = []
    sequence_ready = False
    live_started = False
    settings_saved = False
    original = {}
    result = 0
    window_created = False

    def read_aoi() -> Rect:
        value = Rect()
        check("is_AOI(GET_AOI)", aoi(handle, IS_AOI_IMAGE_GET_AOI,
                                      ctypes.byref(value), ctypes.sizeof(value)))
        return value

    def read_exposure() -> float:
        value = ctypes.c_double()
        check("is_Exposure(GET)", exposure(handle, IS_EXPOSURE_GET,
            ctypes.byref(value), ctypes.sizeof(value)))
        return value.value

    def read_clock() -> int:
        value = c_uint()
        check("is_PixelClock(GET)", pixel_clock(handle, IS_PIXELCLOCK_CMD_GET,
            ctypes.byref(value), ctypes.sizeof(value)))
        return int(value.value)

    def read_configured_fps() -> float:
        value = ctypes.c_double()
        check("is_SetFrameRate(GET)", set_fps(handle,
            ctypes.c_double(IS_GET_FRAMERATE), ctypes.byref(value)))
        return value.value

    def set_clock(value: int) -> None:
        clock_value = c_uint(value)
        check(f"is_PixelClock(SET {value})", pixel_clock(handle,
            IS_PIXELCLOCK_CMD_SET, ctypes.byref(clock_value), ctypes.sizeof(clock_value)))

    def query_color_controls() -> dict:
        # These are read-only GET calls; the experiment never changes driver WB,
        # hardware gains, or color-correction state.
        factor = ctypes.c_double()
        controls = {
            "color_correction_current": int(color_correction(handle, IS_GET_CCOR_MODE, ctypes.byref(factor))),
            "color_correction_supported_mask": int(color_correction(handle, IS_GET_SUPPORTED_CCOR_MODE, ctypes.byref(factor))),
        }
        red, green, blue = ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
        controls["wb_multiplier_rc"] = int(get_wb_multipliers(
            handle, ctypes.byref(red), ctypes.byref(green), ctypes.byref(blue)))
        controls["wb_multipliers_rgb"] = [red.value, green.value, blue.value]
        wb_enabled = ctypes.c_double()
        wb_rc = auto_parameter(handle, IS_GET_ENABLE_AUTO_WHITEBALANCE,
                                ctypes.byref(wb_enabled), None)
        controls["auto_wb_query_rc"] = int(wb_rc)
        controls["auto_wb_enabled"] = wb_enabled.value if wb_rc == IS_SUCCESS else None
        controls["hardware_gain_rgb"] = [
            int(hardware_gain(handle, IS_GET_RED_GAIN, IS_IGNORE_PARAMETER, IS_IGNORE_PARAMETER, IS_IGNORE_PARAMETER)),
            int(hardware_gain(handle, IS_GET_GREEN_GAIN, IS_IGNORE_PARAMETER, IS_IGNORE_PARAMETER, IS_IGNORE_PARAMETER)),
            int(hardware_gain(handle, IS_GET_BLUE_GAIN, IS_IGNORE_PARAMETER, IS_IGNORE_PARAMETER, IS_IGNORE_PARAMETER)),
        ]
        controls["hardware_master_gain"] = int(hardware_gain(
            handle, IS_GET_MASTER_GAIN, IS_IGNORE_PARAMETER, IS_IGNORE_PARAMETER, IS_IGNORE_PARAMETER))
        return controls

    try:
        # Save every setting this experiment can change before writing anything.
        original["binning"] = int(set_binning(handle, IS_GET_BINNING))
        original["aoi"] = read_aoi()
        original["pixel_clock"] = read_clock()
        original["exposure_ms"] = read_exposure()
        original["fps"] = read_configured_fps()
        original["color_mode"] = int(set_color_mode(handle, IS_GET_COLOR_MODE))
        if original["binning"] < 0 or original["color_mode"] < 0 or original["fps"] <= 0:
            raise RuntimeError(f"Could not safely read initial settings: {original}")
        settings_saved = True
        old_aoi: Rect = original["aoi"]
        color_controls = query_color_controls()
        print(f"Channel order: camera mode 1 = RGB24 legacy packed BGR8; mode readback={original['color_mode']}.",
              flush=True)
        print(f"Read-only IDS color controls: {json.dumps(color_controls)}", flush=True)
        print("No automatic per-frame white balance is enabled by this preview; "
              "software gains remain fixed until a slider is moved.", flush=True)

        print(f"Saved settings: mode={original['color_mode']}; binning=0x{original['binning']:04X}; "
              f"AOI=({old_aoi.s32X},{old_aoi.s32Y},{old_aoi.s32Width},{old_aoi.s32Height}); "
              f"clock={original['pixel_clock']} MHz; exposure={original['exposure_ms']:.3f} ms; "
              f"configured FPS={original['fps']:.3f}", flush=True)

        supported_binning = int(set_binning(handle, IS_GET_SUPPORTED_BINNING))
        requested_binning = IS_BINNING_2X_VERTICAL | IS_BINNING_2X_HORIZONTAL
        if supported_binning < 0 or supported_binning & requested_binning != requested_binning:
            raise RuntimeError(f"2x2 binning is not supported (mask=0x{supported_binning:04X})")
        check("is_SetBinning(2x2)", set_binning(handle, requested_binning))
        if int(set_binning(handle, IS_GET_BINNING)) != requested_binning:
            raise RuntimeError("2x2 binning readback did not match")

        # Request BGR8 explicitly. The following query verifies actual current
        # mode; the allocated buffer inquiry verifies actual buffer depth/pitch.
        check("is_SetColorMode(RGB24/BGR8)", set_color_mode(handle, IS_SET_CM_RGB24))
        current_mode = int(set_color_mode(handle, IS_GET_COLOR_MODE))
        if current_mode != IS_SET_CM_RGB24:
            raise RuntimeError(f"Camera did not apply BGR8 mode: readback={current_mode}")
        bits_query = int(set_color_mode(handle, IS_GET_BITS_PER_PIXEL))
        if bits_query not in (24, 174):
            raise RuntimeError(f"Unexpected IS_GET_BITS_PER_PIXEL response {bits_query}; expected 24 or unsupported code 174")

        clock_count = c_uint()
        check("is_PixelClock(GET_NUMBER)", pixel_clock(handle, 1,
            ctypes.byref(clock_count), ctypes.sizeof(clock_count)))
        if not 0 < clock_count.value < 256:
            raise RuntimeError(f"Invalid pixel-clock count {clock_count.value}")
        clocks = (c_uint * clock_count.value)()
        check("is_PixelClock(GET_LIST)", pixel_clock(handle, 2, clocks, ctypes.sizeof(clocks)))
        if 50 not in list(clocks):
            raise RuntimeError(f"50 MHz unavailable in 2x2 mode; clocks={list(clocks)}")
        set_clock(50)
        if read_clock() != 50:
            raise RuntimeError("Pixel clock readback is not 50 MHz")

        requested_exposure = ctypes.c_double(10.0)
        check("is_Exposure(SET 10 ms)", exposure(handle, IS_EXPOSURE_SET,
            ctypes.byref(requested_exposure), ctypes.sizeof(requested_exposure)))
        actual_exposure = read_exposure()

        min_t, max_t, step_t = ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
        check("is_GetFrameTimeRange", get_time_range(handle, ctypes.byref(min_t),
            ctypes.byref(max_t), ctypes.byref(step_t)))
        if min_t.value <= 0:
            raise RuntimeError(f"Invalid minimum frame time {min_t.value}")
        requested_fps = 1.0 / min_t.value
        applied_fps = ctypes.c_double()
        check("is_SetFrameRate(max)", set_fps(handle, requested_fps,
            ctypes.byref(applied_fps)))

        rect = read_aoi()
        if (rect.s32Width, rect.s32Height) != (1024, 768):
            raise RuntimeError(f"2x2 AOI is {rect.s32Width}x{rect.s32Height}, expected 1024x768")

        # The camera's color mode says BGR8, so allocate exactly 24 bits.
        for _ in range(BUFFER_COUNT):
            mem, mem_id = c_void_p(), c_int()
            check("is_AllocImageMem(BGR8)", alloc(handle, rect.s32Width,
                rect.s32Height, 24, ctypes.byref(mem), ctypes.byref(mem_id)))
            buffers.append((mem, mem_id))
            check("is_AddToSequence(BGR8)", add_sequence(handle, mem, mem_id.value))
            sequence_ready = True

        width, height, actual_bits, pitch = c_int(), c_int(), c_int(), c_int()
        check("is_InquireImageMem(BGR8)", inquire(handle, buffers[0][0],
            buffers[0][1].value, ctypes.byref(width), ctypes.byref(height),
            ctypes.byref(actual_bits), ctypes.byref(pitch)))
        if (width.value, height.value, actual_bits.value) != (1024, 768, 24):
            raise RuntimeError(f"BGR8 buffer verification failed: {width.value}x{height.value}, {actual_bits.value} bpp")
        row_min = width.value * 3
        if pitch.value < row_min:
            raise RuntimeError(f"Invalid BGR8 pitch {pitch.value}; minimum is {row_min}")
        frame_bytes = pitch.value * height.value
        print(f"Verified camera mode={current_mode} (RGB24 legacy / packed BGR8); "
              f"bits query={bits_query}; buffer={width.value}x{height.value}, "
              f"{actual_bits.value} bpp, pitch={pitch.value}, row padding={pitch.value-row_min}",
              flush=True)
        print(f"Configured: 2x2, 50 MHz, exposure={actual_exposure:.3f} ms; "
              f"max driver rate={applied_fps.value:.3f} FPS "
              f"(min frame time={min_t.value*1000:.3f} ms)", flush=True)

        root = Path(__file__).resolve().parents[1]
        output_dir = root / "captures" / f"bgr8_color_compare_2x2_50mhz_{datetime.now():%Y%m%d_%H%M%S}"
        output_dir.mkdir(parents=True, exist_ok=False)
        pointer_to_id = {mem.value: mem_id.value for mem, mem_id in buffers}
        check("is_CaptureVideo", capture(handle, IS_DONT_WAIT))
        live_started = True
        freshness_delay = max(0.25, 2.5 / applied_fps.value)
        time.sleep(freshness_delay)

        cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WINDOW, 1200, 930)
        window_created = True
        # Fixed starting correction measured from the previously selected
        # neutral patch (BGR medians 109, 168, 108). This lowers the green
        # channel and modestly raises red/blue; sliders can still adjust it.
        color_gains = {"b": 1.15, "g": 0.75, "r": 1.16}
        gain_names = {"b": "Blue gain x100", "g": "Green gain x100", "r": "Red gain x100"}

        def make_gain_callback(channel: str):
            def update(position: int) -> None:
                color_gains[channel] = (position + 25) / 100.0
            return update

        initial_gain_positions = {"b": 90, "g": 50, "r": 91}
        for channel in ("b", "g", "r"):
            # Slider positions 0..375 represent fixed gains 0.25..4.00.
            cv2.createTrackbar(gain_names[channel], WINDOW,
                               initial_gain_positions[channel], 375,
                               make_gain_callback(channel))

        def set_gain(channel: str, gain: float) -> None:
            gain = max(0.25, min(4.0, float(gain)))
            cv2.setTrackbarPos(gain_names[channel], WINDOW,
                               int(round(gain * 100)) - 25)

        def calibrate_from_roi(image: np.ndarray, x: int, y: int,
                               roi_width: int, roi_height: int) -> dict:
            if x < 0 or y < 0 or x + roi_width > width.value or y + roi_height > height.value:
                raise RuntimeError(f"neutral ROI {(x, y, roi_width, roi_height)} is outside the frame")
            patch = image[y:y + roi_height, x:x + roi_width].astype(np.float32)
            luma = patch.mean(axis=2)
            valid = (luma > 12.0) & (luma < 245.0)
            if int(valid.sum()) < 25:
                raise RuntimeError("neutral ROI has too few unsaturated pixels; choose a brighter neutral patch")
            channel_medians = np.median(patch[valid], axis=0)  # B,G,R order
            if np.any(channel_medians <= 1.0):
                raise RuntimeError(f"neutral ROI has a near-zero channel: {channel_medians.tolist()}")
            target = float(np.exp(np.log(channel_medians).mean()))
            computed = np.clip(target / channel_medians, 0.25, 4.0)
            for channel, gain in zip(("b", "g", "r"), computed):
                set_gain(channel, float(gain))
            return {
                "roi_xywh": [x, y, roi_width, roi_height],
                "sample_median_bgr": channel_medians.tolist(),
                "target_level": target,
                "fixed_gains_bgr": [float(color_gains[k]) for k in ("b", "g", "r")],
            }

        shown_times: list[float] = []
        intervals: deque[float] = deque(maxlen=300)
        previous_ptr = None
        first_frame_saved = False
        last_report = time.perf_counter()
        displayed = 0
        latest_driver_fps = ctypes.c_double(float("nan"))
        calibration_done = False
        calibration_metadata = None
        metadata_path = output_dir / "metadata.json"
        deadline = time.perf_counter() + args.seconds if args.seconds else None
        print(f"Preview running. Raw unmodified frame saved in: {output_dir}. "
              "Showing the fixed-gain color-corrected view only; press Q/Esc to stop.",
              flush=True)
        print(f"Initial fixed software gains B/G/R="
              f"{color_gains['b']:.2f}/{color_gains['g']:.2f}/{color_gains['r']:.2f}.", flush=True)

        while deadline is None or time.perf_counter() < deadline:
            active_id, current_ptr, last_ptr = c_int(), c_void_p(), c_void_p()
            rc = get_active(handle, ctypes.byref(active_id),
                            ctypes.byref(current_ptr), ctypes.byref(last_ptr))
            ptr = last_ptr.value
            if rc == IS_SUCCESS and ptr and ptr != previous_ptr:
                mem_id = pointer_to_id.get(ptr)
                if mem_id is not None and lock(handle, mem_id, c_void_p(ptr)) == IS_SUCCESS:
                    payload = ctypes.string_at(ptr, frame_bytes)
                    check("is_UnlockSeqBuf", unlock(handle, mem_id, c_void_p(ptr)))
                    previous_ptr = ptr
                    # The exact buffer bytes are saved before any preview work.
                    rows = np.frombuffer(payload, dtype=np.uint8).reshape(height.value, pitch.value)
                    bgr = rows[:, :width.value * 3].reshape(height.value, width.value, 3)
                    if not first_frame_saved:
                        (output_dir / "frame_unmodified_bgr8.raw").write_bytes(payload)
                        metadata = {
                            "camera_serial": TARGET_SERIAL,
                            "camera_color_mode": current_mode,
                            "mode_name": "RGB24 legacy alias; packed BGR8",
                            "channel_order": "B,G,R",
                            "bits_per_pixel": actual_bits.value,
                            "bits_query_result": bits_query,
                            "width": width.value, "height": height.value,
                            "pitch_bytes_per_row": pitch.value,
                            "frame_bytes_including_row_padding": frame_bytes,
                            "binning": "2x2", "pixel_clock_mhz": 50,
                            "exposure_ms": actual_exposure,
                            "configured_fps": applied_fps.value,
                            "ids_color_controls_read_only": color_controls,
                            "raw_file": "frame_unmodified_bgr8.raw",
                            "raw_payload_is_unmodified": True,
                            "software_gain_adjustment": "fixed per channel; no per-frame auto adjustment",
                        }
                        first_frame_saved = True
                        print(f"Saved exact raw BGR8 frame: {output_dir / 'frame_unmodified_bgr8.raw'}",
                              flush=True)

                    if not calibration_done:
                        if args.calibrate_once:
                            cv2.imshow(ROI_WINDOW, bgr)
                            x, y, rw, rh = cv2.selectROI(
                                ROI_WINDOW, bgr, showCrosshair=False, fromCenter=False)
                            try:
                                cv2.destroyWindow(ROI_WINDOW)
                            except cv2.error:
                                pass
                            if rw > 0 and rh > 0:
                                calibration_metadata = calibrate_from_roi(bgr, x, y, rw, rh)
                                print(f"One-time neutral-patch calibration: {json.dumps(calibration_metadata)}",
                                      flush=True)
                            else:
                                print("Calibration cancelled; keeping unity gains.", flush=True)
                        elif roi is not None:
                            calibration_metadata = calibrate_from_roi(bgr, *roi)
                            print(f"One-time neutral-patch calibration: {json.dumps(calibration_metadata)}",
                                  flush=True)
                        calibration_done = True

                    gains = np.array([color_gains["b"], color_gains["g"],
                                      color_gains["r"]], dtype=np.float32)
                    # Optimized uint8 transform avoids a full-frame float32 intermediate.
                    corrected = cv2.transform(bgr, np.diag(gains))
                    # Show only the fixed-gain corrected view. Keep the original
                    # camera bytes unchanged in the raw capture file above.
                    panel = np.zeros((height.value + 34, width.value, 3), dtype=np.uint8)
                    panel[34:, :] = corrected
                    cv2.putText(panel, f"Color-corrected BGR8 | fixed gains B={color_gains['b']:.2f} G={color_gains['g']:.2f} R={color_gains['r']:.2f}",
                                (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65,
                                (255, 255, 255), 1, cv2.LINE_AA)
                    # Fit the single 4:3 camera image on a typical display.
                    display_width = 1200
                    display_height = round((height.value + 34) * display_width / width.value)
                    panel = cv2.resize(panel, (display_width, display_height),
                                       interpolation=cv2.INTER_AREA)
                    cv2.imshow(WINDOW, panel)
                    key = cv2.waitKey(1) & 0xFF
                    now = time.perf_counter()
                    if shown_times:
                        intervals.append((now - shown_times[-1]) * 1000.0)
                    shown_times.append(now)
                    displayed += 1
                    if key in (27, ord("q"), ord("Q")):
                        break
            else:
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q"), ord("Q")):
                    break

            try:
                if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    break
            except cv2.error:
                break

            get_fps(handle, ctypes.byref(latest_driver_fps))
            now = time.perf_counter()
            if now - last_report >= 2.0:
                recent = list(intervals)
                rolling = 1000.0 / statistics.mean(recent) if recent else 0.0
                print(f"displayed={displayed}; displayed_fps={rolling:.2f}; "
                      f"driver_fps={latest_driver_fps.value:.2f}; "
                      f"interval_median_ms={statistics.median(recent) if recent else float('nan'):.2f}; "
                      f"interval_p95_ms={percentile(recent, .95):.2f}", flush=True)
                last_report = now
            if ptr == previous_ptr:
                time.sleep(0.0005)

        if not first_frame_saved:
            raise RuntimeError("Live capture ended before a complete raw frame was saved")
        recent = list(intervals)
        if len(shown_times) > 1:
            measured = (len(shown_times) - 1) / (shown_times[-1] - shown_times[0])
        else:
            measured = 0.0
        print(f"Preview ended: displayed={displayed}, displayed_fps={measured:.2f}; "
              f"interval_median_ms={statistics.median(recent) if recent else float('nan'):.2f}; "
              f"interval_p95_ms={percentile(recent, .95):.2f}", flush=True)
        final_gains = {key: round(value, 4) for key, value in color_gains.items()}
        if first_frame_saved:
            metadata["final_fixed_software_gains_bgr"] = [
                final_gains["b"], final_gains["g"], final_gains["r"]]
            metadata["one_time_neutral_calibration"] = calibration_metadata
            metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        print(f"Final fixed software gains (B/G/R): {final_gains['b']:.2f}/"
              f"{final_gains['g']:.2f}/{final_gains['r']:.2f}", flush=True)
        if measured < 33.0:
            print("Note: displayed rate was below approximately 35 FPS; see driver rate and interval reports.",
                  flush=True)

    except KeyboardInterrupt:
        print("Interrupted; cleaning up camera resources.", flush=True)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        result = 5
    finally:
        if window_created:
            try:
                cv2.destroyWindow(ROI_WINDOW)
            except cv2.error:
                pass
            try:
                cv2.destroyWindow(WINDOW)
            except cv2.error:
                pass
        if live_started:
            print(f"is_StopLiveVideo: {stop(handle, IS_WAIT)}", flush=True)
        if sequence_ready:
            print(f"is_ClearSequence: {clear_sequence(handle)}", flush=True)
        for mem, mem_id in buffers:
            rc = free_mem(handle, mem, mem_id.value)
            if rc != IS_SUCCESS:
                print(f"WARNING: is_FreeImageMem returned {rc}", flush=True)
        if settings_saved:
            try:
                # Restore mode and binning before AOI/timing values. Verify by
                # reading every setting back after restoration.
                check("restore color mode", set_color_mode(handle, original["color_mode"]))
                check("restore binning", set_binning(handle, original["binning"]))
                saved_aoi: Rect = original["aoi"]
                current_aoi = read_aoi()
                saved_tuple = (saved_aoi.s32X, saved_aoi.s32Y,
                               saved_aoi.s32Width, saved_aoi.s32Height)
                current_tuple = (current_aoi.s32X, current_aoi.s32Y,
                                 current_aoi.s32Width, current_aoi.s32Height)
                if current_tuple != saved_tuple:
                    check("restore AOI", aoi(handle, IS_AOI_IMAGE_SET_AOI,
                        ctypes.byref(saved_aoi), ctypes.sizeof(saved_aoi)))
                set_clock(original["pixel_clock"])
                # Restore the original slow rate before restoring its long exposure.
                restored_fps = ctypes.c_double()
                check("restore frame rate", set_fps(handle, original["fps"],
                    ctypes.byref(restored_fps)))
                requested_exp = ctypes.c_double(original["exposure_ms"])
                check("restore exposure", exposure(handle, IS_EXPOSURE_SET,
                    ctypes.byref(requested_exp), ctypes.sizeof(requested_exp)))
                restored = {
                    "color_mode": int(set_color_mode(handle, IS_GET_COLOR_MODE)),
                    "binning": int(set_binning(handle, IS_GET_BINNING)),
                    "aoi": read_aoi(),
                    "pixel_clock": read_clock(),
                    "exposure_ms": read_exposure(),
                    "fps": read_configured_fps(),
                }
                r = restored["aoi"]
                print(f"Restored/readback: mode={restored['color_mode']}; "
                      f"binning=0x{restored['binning']:04X}; "
                      f"AOI=({r.s32X},{r.s32Y},{r.s32Width},{r.s32Height}); "
                      f"clock={restored['pixel_clock']} MHz; "
                      f"exposure={restored['exposure_ms']:.3f} ms; "
                      f"configured FPS={restored['fps']:.3f}", flush=True)
                if (restored["color_mode"] != original["color_mode"] or
                    restored["binning"] != original["binning"] or
                    (r.s32X, r.s32Y, r.s32Width, r.s32Height) != saved_tuple or
                    restored["pixel_clock"] != original["pixel_clock"] or
                    abs(restored["exposure_ms"] - original["exposure_ms"]) > 0.1 or
                    abs(restored["fps"] - original["fps"]) > 0.1):
                    raise RuntimeError("One or more restored camera settings differ from the original")
            except Exception as exc:
                print(f"WARNING: setting restoration failed: {exc}", file=sys.stderr)
                result = 6
        if initialized:
            print(f"is_ExitCamera: {exit_camera(handle)}", flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
