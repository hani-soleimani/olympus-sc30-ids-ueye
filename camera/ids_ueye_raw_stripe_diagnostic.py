r"""Read-only single-frame IDS uEye diagnostic for fixed vertical stripes.

The script preserves the camera's current settings. It captures one frame into
an IDS sequence buffer, saves the buffer bytes verbatim, then writes separate
channel/conversion/display-scale images and column profiles. It does not apply
denoising, enhancement, or geometric correction.

Run from PowerShell after closing DP2 and all other camera clients:
    .\.venv\Scripts\python.exe camera\ids_ueye_raw_stripe_diagnostic.py
"""

from __future__ import annotations

import argparse
import ctypes
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from ctypes import c_char, c_int, c_int32, c_uint, c_uint32, c_void_p

try:
    import cv2
    import numpy as np
except ImportError as exc:
    raise SystemExit(f"NumPy and OpenCV are required: {exc}")


DLL_PATH = r"C:\Windows\System32\uc480_64.dll"
TARGET_SERIAL = "0057004412"
IS_SUCCESS = 0
IS_DONT_WAIT = 0
IS_WAIT = 1
IS_AOI_IMAGE_GET_AOI = 2
IS_GET_COLOR_MODE = 0x8000
IS_GET_BITS_PER_PIXEL = 0x8001
IS_SET_CM_RGB32 = 0
IS_SET_CM_RGB24 = 1
IS_SET_CM_Y8 = 6
IS_SET_CM_BAYER = 11
IS_GET_BINNING = 0x8000
IS_PIXELCLOCK_CMD_GET = 5
IS_EXPOSURE_GET = 7
BUFFER_COUNT = 3
DISPLAY_SCALE = 1.38


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


def dominant_period(profile: np.ndarray) -> tuple[float, float]:
    """Return dominant detrended column period (px) and sinusoid amplitude."""
    profile = np.asarray(profile, dtype=np.float64)
    n = profile.size
    if n < 64:
        return float("nan"), float("nan")
    # Remove broad illumination/vignetting changes while retaining fine bands.
    window = min(81, n if n % 2 else n - 1)
    baseline = np.convolve(profile, np.ones(window) / window, mode="same")
    detrended = profile - baseline
    spectrum = np.abs(np.fft.rfft(detrended))
    frequencies = np.fft.rfftfreq(n, d=1.0)
    usable = (frequencies >= 1.0 / 64.0) & (frequencies <= 0.5)
    if not np.any(usable):
        return float("nan"), float("nan")
    indices = np.flatnonzero(usable)
    peak = indices[int(np.argmax(spectrum[usable]))]
    freq = frequencies[peak]
    amplitude = 2.0 * spectrum[peak] / n
    return (float(1.0 / freq), float(amplitude)) if freq else (float("nan"), float("nan"))


def append_profiles(rows: list[dict], label: str, image: np.ndarray,
                    channels: list[str]) -> None:
    """Add per-column mean/std and periodicity stats for a HxWxC image."""
    if image.ndim == 2:
        planes = [("gray", image)]
    else:
        planes = [(name, image[:, :, i]) for i, name in enumerate(channels)]
    for channel, plane in planes:
        means = plane.astype(np.float64).mean(axis=0)
        stds = plane.astype(np.float64).std(axis=0)
        period, amplitude = dominant_period(means)
        for x, (mean, std) in enumerate(zip(means, stds)):
            rows.append({
                "source": label, "channel": channel, "column_x": x,
                "mean_intensity": f"{mean:.6f}", "within_column_std": f"{std:.6f}",
                "profile_period_px": f"{period:.6f}",
                "profile_period_amplitude": f"{amplitude:.6f}",
            })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=TARGET_SERIAL)
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="output folder; defaults to timestamped folder under captures/")
    parser.add_argument("--display", action="store_true",
                        help="show raw-plane and converted views; press Q/Esc to close")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    output_dir = args.output_dir or root / "captures" / f"raw_stripe_{datetime.now():%Y%m%d_%H%M%S}"
    try:
        dll = ctypes.WinDLL(DLL_PATH)
    except OSError as exc:
        print(f"ERROR loading {DLL_PATH}: {exc}", file=sys.stderr)
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
        raise RuntimeError(f"camera {args.serial} is already in use; close its preview first")
    output_dir.mkdir(parents=True, exist_ok=False)

    init = bind(dll, "is_InitCamera", [ctypes.POINTER(c_int), c_void_p])
    exit_camera = bind(dll, "is_ExitCamera", [c_int])
    get_color_depth = bind(dll, "is_GetColorDepth", [c_int,
        ctypes.POINTER(c_int), ctypes.POINTER(c_int)])
    set_color_mode = bind(dll, "is_SetColorMode", [c_int, c_int])
    frame_rate = bind(dll, "is_SetFrameRate", [c_int, ctypes.c_double,
        ctypes.POINTER(ctypes.c_double)])
    get_aoi = bind(dll, "is_AOI", [c_int, c_uint, c_void_p, c_uint])
    binning = bind(dll, "is_SetBinning", [c_int, c_int])
    pixel_clock = bind(dll, "is_PixelClock", [c_int, c_uint, c_void_p, c_uint])
    exposure = bind(dll, "is_Exposure", [c_int, c_uint, c_void_p, c_uint])
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
    buffers: list[tuple[c_void_p, c_int]] = []
    sequence_ready = live_started = False
    result = 0
    try:
        # Query current state only. No camera setting is written by this script.
        aoi = Rect()
        check("is_AOI(GET_AOI)", get_aoi(handle, IS_AOI_IMAGE_GET_AOI,
                                           ctypes.byref(aoi), ctypes.sizeof(aoi)))
        desktop_bits, desktop_mode = c_int(), c_int()
        check("is_GetColorDepth (desktop format)", get_color_depth(handle,
            ctypes.byref(desktop_bits), ctypes.byref(desktop_mode)))
        queried_mode = set_color_mode(handle, IS_GET_COLOR_MODE)
        queried_format_bits = set_color_mode(handle, IS_GET_BITS_PER_PIXEL)
        binning_mask = binning(handle, IS_GET_BINNING)
        clock = c_uint()
        clock_rc = pixel_clock(handle, IS_PIXELCLOCK_CMD_GET,
                               ctypes.byref(clock), ctypes.sizeof(clock))
        exposure_ms = ctypes.c_double()
        exposure_rc = exposure(handle, IS_EXPOSURE_GET, ctypes.byref(exposure_ms),
                               ctypes.sizeof(exposure_ms))
        if aoi.s32Width <= 0 or aoi.s32Height <= 0:
            raise RuntimeError(f"invalid camera AOI: {aoi.s32Width}x{aoi.s32Height}")
        mode_to_bits = {IS_SET_CM_RGB32: 32, IS_SET_CM_RGB24: 24,
                        IS_SET_CM_Y8: 8, IS_SET_CM_BAYER: 8}
        if queried_mode not in mode_to_bits:
            raise RuntimeError(f"unrecognized current camera color mode {queried_mode}; refusing to guess a buffer format")
        print(f"Current settings (read only): AOI={aoi.s32Width}x{aoi.s32Height}; "
              f"binning_mask=0x{binning_mask:04X}; pixel_clock={clock.value if clock_rc == 0 else 'unavailable'} MHz; "
              f"exposure={exposure_ms.value if exposure_rc == 0 else 'unavailable'} ms")
        print(f"Desktop format (not camera format): is_GetColorDepth={desktop_bits.value} bpp / "
              f"mode={desktop_mode.value}; camera is_SetColorMode(IS_GET_COLOR_MODE)={queried_mode}; "
              f"camera-format bits query={queried_format_bits}")

        # This installed IDS version returns 174 (unsupported) for the newer
        # IS_GET_BITS_PER_PIXEL query. Infer only from the documented current
        # mode enum; do not use the Windows desktop depth for camera storage.
        allocation_bits = queried_format_bits if queried_format_bits in (8, 16, 24, 32) else mode_to_bits[queried_mode]
        if allocation_bits != mode_to_bits[queried_mode]:
            raise RuntimeError(f"camera mode {queried_mode} conflicts with queried format depth {allocation_bits}; no capture attempted")
        if allocation_bits not in (8, 16, 24, 32):
            raise RuntimeError(f"unsupported/unknown buffer depth {allocation_bits} bpp; raw frame not allocated")
        for _ in range(BUFFER_COUNT):
            mem, mem_id = c_void_p(), c_int()
            check("is_AllocImageMem", alloc(handle, aoi.s32Width, aoi.s32Height,
                allocation_bits, ctypes.byref(mem), ctypes.byref(mem_id)))
            buffers.append((mem, mem_id))
            check("is_AddToSequence", add_sequence(handle, mem, mem_id.value))
            sequence_ready = True
        width, height, actual_bits, pitch = c_int(), c_int(), c_int(), c_int()
        check("is_InquireImageMem", inquire(handle, buffers[0][0], buffers[0][1].value,
            ctypes.byref(width), ctypes.byref(height), ctypes.byref(actual_bits), ctypes.byref(pitch)))
        if (width.value, height.value, actual_bits.value) != (aoi.s32Width, aoi.s32Height, allocation_bits):
            raise RuntimeError(f"image-buffer mismatch: {width.value}x{height.value}, {actual_bits.value} bpp; requested allocation {allocation_bits} bpp")
        minimum_row_bytes = (width.value * actual_bits.value + 7) // 8
        if pitch.value < minimum_row_bytes:
            raise RuntimeError(f"invalid pitch {pitch.value}; minimum row bytes={minimum_row_bytes}")
        frame_bytes = pitch.value * height.value
        print(f"Buffer verified: {width.value}x{height.value}; depth={actual_bits.value} bpp; "
              f"pitch={pitch.value} bytes/row; frame bytes={frame_bytes}; row padding={pitch.value-minimum_row_bytes} bytes")

        pointer_to_id = {mem.value: mem_id.value for mem, mem_id in buffers}
        check("is_CaptureVideo", capture(handle, IS_DONT_WAIT))
        live_started = True
        # Do not mistake the zero-initialized sequence buffer for a captured
        # frame. Allow several configured frame periods for a fresh exposure.
        configured_fps = ctypes.c_double()
        fps_rc = frame_rate(handle, ctypes.c_double(0x8000), ctypes.byref(configured_fps))
        frame_period = 1.0 / configured_fps.value if fps_rc == IS_SUCCESS and configured_fps.value > 0 else 0.2
        freshness_delay = max(0.25, 2.5 * frame_period)
        print(f"Waiting {freshness_delay:.3f}s for completed exposures before reading the latest sequence buffer.")
        time.sleep(freshness_delay)
        deadline = time.perf_counter() + 10.0
        previous_ptr = None
        payload = None
        while time.perf_counter() < deadline:
            active_id, active_ptr, last_ptr = c_int(), c_void_p(), c_void_p()
            rc = get_active(handle, ctypes.byref(active_id), ctypes.byref(active_ptr), ctypes.byref(last_ptr))
            ptr = last_ptr.value
            if rc == IS_SUCCESS and ptr and ptr != previous_ptr and ptr in pointer_to_id:
                mem_id = pointer_to_id[ptr]
                if lock(handle, mem_id, c_void_p(ptr)) == IS_SUCCESS:
                    payload = ctypes.string_at(ptr, frame_bytes)
                    check("is_UnlockSeqBuf", unlock(handle, mem_id, c_void_p(ptr)))
                    break
            time.sleep(0.001)
        if payload is None:
            raise RuntimeError("No complete IDS frame arrived within 10 seconds")

        # Preserve every byte, including any row padding, before interpreting it.
        raw_path = output_dir / "frame_unmodified.raw"
        raw_path.write_bytes(payload)
        row_bytes = np.frombuffer(payload, dtype=np.uint8).reshape(height.value, pitch.value)
        np.save(output_dir / "frame_unmodified_rows.npy", row_bytes, allow_pickle=False)
        metadata = {
            "camera_serial": args.serial,
            "model": decode(camera.Model),
            "aoi_x": aoi.s32X, "aoi_y": aoi.s32Y,
            "width_px": width.value, "height_px": height.value,
            "bits_per_pixel": actual_bits.value,
            "is_GetColorDepth_desktop_bits_per_pixel": desktop_bits.value,
            "is_GetColorDepth_desktop_mode": desktop_mode.value,
            "is_SetColorMode_IS_GET_COLOR_MODE_return": queried_mode,
            "is_SetColorMode_IS_GET_COLOR_MODE_constant": IS_GET_COLOR_MODE,
            "is_SetColorMode_RGB32_constant": IS_SET_CM_RGB32,
            "is_SetColorMode_RGB24_constant": IS_SET_CM_RGB24,
            "is_SetColorMode_Y8_constant": IS_SET_CM_Y8,
            "is_SetColorMode_BAYER_constant": IS_SET_CM_BAYER,
            "is_SetColorMode_IS_GET_BITS_PER_PIXEL_return": queried_format_bits,
            "camera_mode_enum_name": {0: "IS_SET_CM_RGB32 / BGRA8", 1: "IS_SET_CM_RGB24 / BGR8", 6: "IS_SET_CM_Y8 / MONO8", 11: "IS_SET_CM_BAYER / SENSOR_RAW8"}[queried_mode],
            "buffer_allocation_bits_per_pixel": allocation_bits,
            "documented_byte_order_for_current_mode": "BGRA8" if queried_mode == IS_SET_CM_RGB32 else ("BGR8" if queried_mode == IS_SET_CM_RGB24 else "single byte plane"),
            "channel_order_verified_by_known_color_target": False,
            "pixel_clock_mhz": clock.value if clock_rc == 0 else None,
            "exposure_ms": exposure_ms.value if exposure_rc == 0 else None,
            "binning_mask": binning_mask,
            "pitch_bytes_per_row": pitch.value,
            "minimum_pixel_bytes_per_row": minimum_row_bytes,
            "row_padding_bytes": pitch.value - minimum_row_bytes,
            "frame_bytes_exact": frame_bytes,
            "raw_file": raw_path.name,
            "raw_file_sha256_note": "Exact payload copied from locked IDS sequence buffer; no pixel transform.",
            "capture_mode": "continuous IDS sequence; latest completed buffer",
            "freshness_wait_seconds": freshness_delay,
            "configured_fps_query": configured_fps.value if fps_rc == IS_SUCCESS else None,
        }

        rows: list[dict] = []
        converted_path = None
        display_path = None
        if actual_bits.value == 32:
            pixel_bytes = row_bytes[:, :width.value * 4].reshape(height.value, width.value, 4)
            if queried_mode == IS_SET_CM_RGB32:
                # IDS documents legacy RGB32 as BGRA8 packed in current uEye APIs.
                channel_names = ["B", "G", "R", "A"]
                bgr = cv2.cvtColor(pixel_bytes, cv2.COLOR_BGRA2BGR)
                gray = cv2.cvtColor(pixel_bytes, cv2.COLOR_BGRA2GRAY)
                for idx, name in enumerate(channel_names):
                    cv2.imwrite(str(output_dir / f"raw_byte_plane_{idx}_{name}.png"), pixel_bytes[:, :, idx])
                converted_path = output_dir / "display_conversion_bgra_to_bgr.png"
                cv2.imwrite(str(converted_path), bgr)
                scaled = cv2.resize(bgr, (round(width.value * DISPLAY_SCALE),
                    round(height.value * DISPLAY_SCALE)), interpolation=cv2.INTER_LINEAR)
                display_path = output_dir / "display_scaled_1p38x.png"
                cv2.imwrite(str(display_path), scaled)
                scaled_gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
                append_profiles(rows, "raw_bytes_as_BGRA", pixel_bytes, channel_names)
                append_profiles(rows, "converted_BGR", bgr, ["B", "G", "R"])
                append_profiles(rows, "converted_gray", gray, ["gray"])
                append_profiles(rows, "display_scaled_gray_1p38x", scaled_gray, ["gray"])
                metadata.update({
                    "pixel_format_interpretation": "IDS-reported 32-bit mode 0; documented legacy RGB32 alias is packed BGRA8",
                    "channel_order_used_for_conversion": "BGRA bytes -> OpenCV BGR image",
                    "conversion_file": converted_path.name,
                    "display_scaled_file": display_path.name,
                    "display_scale": DISPLAY_SCALE,
                })
            elif queried_mode == IS_SET_CM_RGB24:
                metadata["pixel_format_interpretation"] = "RGB24 mode reported but 32-bit image memory allocated; raw bytes retained, packed 24-bit interpretation not assumed"
                metadata["channel_order_used_for_conversion"] = None
                append_profiles(rows, "raw_32bit_byte_planes_unknown_order", pixel_bytes,
                                ["byte0", "byte1", "byte2", "byte3"])
            else:
                metadata["pixel_format_interpretation"] = f"unrecognized 32-bit mode {queried_mode}; no color conversion assumed"
                metadata["channel_order_used_for_conversion"] = None
                append_profiles(rows, "raw_32bit_byte_planes_unknown_order", pixel_bytes,
                                ["byte0", "byte1", "byte2", "byte3"])
        elif actual_bits.value == 24:
            if queried_mode == IS_SET_CM_RGB24:
                # IDS legacy RGB24 maps to packed BGR8 in memory.
                pixel_bytes = row_bytes[:, :width.value * 3].reshape(height.value, width.value, 3)
                channel_names = ["B", "G", "R"]
                bgr = pixel_bytes.copy()
                gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                for idx, name in enumerate(channel_names):
                    cv2.imwrite(str(output_dir / f"raw_byte_plane_{idx}_{name}.png"), pixel_bytes[:, :, idx])
                converted_path = output_dir / "display_conversion_bgr24_to_bgr.png"
                cv2.imwrite(str(converted_path), bgr)
                scaled = cv2.resize(bgr, (round(width.value * DISPLAY_SCALE),
                    round(height.value * DISPLAY_SCALE)), interpolation=cv2.INTER_LINEAR)
                display_path = output_dir / "display_scaled_1p38x.png"
                cv2.imwrite(str(display_path), scaled)
                append_profiles(rows, "raw_bytes_as_BGR", pixel_bytes, channel_names)
                append_profiles(rows, "converted_BGR", bgr, channel_names)
                append_profiles(rows, "converted_gray", gray, ["gray"])
                append_profiles(rows, "display_scaled_gray_1p38x",
                                cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY), ["gray"])
                metadata.update({
                    "pixel_format_interpretation": "IDS mode 1 legacy RGB24 maps to packed BGR8",
                    "channel_order_used_for_conversion": "BGR bytes copied to OpenCV BGR image",
                    "conversion_file": converted_path.name,
                    "display_scaled_file": display_path.name,
                    "display_scale": DISPLAY_SCALE,
                })
            else:
                metadata["pixel_format_interpretation"] = f"24-bit buffer with unrecognized mode {queried_mode}; no channel order assumed"
                append_profiles(rows, "raw_24bit_bytes_unknown_order",
                                row_bytes[:, :width.value * 3].reshape(height.value, width.value, 3),
                                ["byte0", "byte1", "byte2"])
        elif actual_bits.value == 8:
            pixels = row_bytes[:, :width.value]
            cv2.imwrite(str(output_dir / "raw_mono8.png"), pixels)
            append_profiles(rows, "raw_8bit_plane", pixels, ["raw"])
            metadata["pixel_format_interpretation"] = "8-bit single-plane; mode distinguishes monochrome from raw Bayer"
        else:
            metadata["pixel_format_interpretation"] = "multibyte format saved byte-for-byte; no unverified unpacking/conversion attempted"

        profile_path = output_dir / "column_intensity_profiles.csv"
        with profile_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["source", "channel", "column_x",
                "mean_intensity", "within_column_std", "profile_period_px",
                "profile_period_amplitude"])
            writer.writeheader()
            writer.writerows(rows)
        metadata["column_profile_csv"] = profile_path.name
        metadata_path = output_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        print(f"Saved exact IDS buffer: {raw_path}")
        print(f"Saved metadata and profiles under: {output_dir}")
        print(f"Captured format: mode={queried_mode}, {actual_bits.value} bpp, "
              f"{width.value}x{height.value}, pitch={pitch.value}; files are unmodified raw plus separate analysis outputs.")

        if args.display and converted_path is not None:
            # Compare native and display-scaled output. This window is not used
            # for the saved raw frame or intensity calculations.
            native = cv2.imread(str(converted_path))
            enlarged = cv2.imread(str(display_path))
            view = np.zeros((max(native.shape[0], enlarged.shape[0]),
                             native.shape[1] + enlarged.shape[1], 3), dtype=np.uint8)
            view[:native.shape[0], :native.shape[1]] = native
            view[:enlarged.shape[0], native.shape[1]:] = enlarged
            cv2.putText(view, "Native converted", (10, 28), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (0, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(view, "1.38x display raster", (native.shape[1] + 10, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)
            cv2.namedWindow("IDS raw-stripe diagnostic (Q/Esc closes)", cv2.WINDOW_NORMAL)
            cv2.imshow("IDS raw-stripe diagnostic (Q/Esc closes)", view)
            while (cv2.waitKey(50) & 0xFF) not in (27, ord("q"), ord("Q")):
                try:
                    if cv2.getWindowProperty("IDS raw-stripe diagnostic (Q/Esc closes)", cv2.WND_PROP_VISIBLE) < 1:
                        break
                except cv2.error:
                    break
    except KeyboardInterrupt:
        print("Interrupted; releasing the IDS stream and buffers.")
        result = 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        result = 5
    finally:
        if live_started:
            print(f"is_StopLiveVideo: {stop(handle, IS_WAIT)}")
        if sequence_ready:
            print(f"is_ClearSequence: {clear_sequence(handle)}")
        for mem, mem_id in buffers:
            rc = free_mem(handle, mem, mem_id.value)
            if rc != IS_SUCCESS:
                print(f"WARNING: is_FreeImageMem returned {rc}")
        cv2.destroyAllWindows()
        print(f"is_ExitCamera: {exit_camera(handle)}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
