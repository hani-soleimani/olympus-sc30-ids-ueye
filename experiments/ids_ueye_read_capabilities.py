"""Read-only IDS uEye capability query for the Olympus SC30.

This opens the installed IDS-compatible camera API only long enough to query
the present AOI, AOI limits/increments, supported binning flags, exposure range,
and frame-time limits. It does not call any setting function or start capture.

Run after closing DP2 and any other camera client:
    .\\.venv\\Scripts\\python.exe experiments\\ids_ueye_read_capabilities.py
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import c_char, c_int, c_int32, c_uint, c_uint32, c_void_p

DLL_PATH = r"C:\Windows\System32\uc480_64.dll"
TARGET_SERIAL = "0057004412"
IS_SUCCESS = 0

IS_AOI_IMAGE_GET_AOI = 0x0002
IS_AOI_IMAGE_GET_SIZE_MIN = 0x0008
IS_AOI_IMAGE_GET_SIZE_MAX = 0x0010
IS_AOI_IMAGE_GET_SIZE_INC = 0x0012
IS_AOI_IMAGE_GET_POS_MIN = 0x0007
IS_AOI_IMAGE_GET_POS_MAX = 0x0009
IS_AOI_IMAGE_GET_POS_INC = 0x0011
IS_AOI_IMAGE_GET_ORIGINAL_AOI = 0x0015

IS_GET_SUPPORTED_BINNING = 0x8001
IS_GET_BINNING = 0x8000
IS_GET_BINNING_TYPE = 0x8002
IS_GET_BINNING_FACTOR_HORIZONTAL = 0x8004
IS_GET_BINNING_FACTOR_VERTICAL = 0x8008

IS_EXPOSURE_CMD_GET_EXPOSURE = 7
IS_EXPOSURE_CMD_GET_EXPOSURE_RANGE_MIN = 0x8000
IS_EXPOSURE_CMD_GET_EXPOSURE_RANGE_MAX = 0x8001
IS_EXPOSURE_CMD_GET_EXPOSURE_RANGE_INC = 0x8002


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


class Size2D(ctypes.Structure):
    _fields_ = [("s32Width", c_int32), ("s32Height", c_int32)]


class Point2D(ctypes.Structure):
    _fields_ = [("s32X", c_int32), ("s32Y", c_int32)]


def decode(value: bytes) -> str:
    return value.split(b"\0", 1)[0].decode("ascii", "replace")


def bind(dll, name: str, argtypes: list[object], restype=c_int):
    fn = getattr(dll, name)
    fn.argtypes = argtypes
    fn.restype = restype
    return fn


def main() -> int:
    try:
        dll = ctypes.WinDLL(DLL_PATH)
    except OSError as exc:
        print(f"ERROR: cannot load {DLL_PATH}: {exc}", file=sys.stderr)
        return 2

    get_count = bind(dll, "is_GetNumberOfCameras", [ctypes.POINTER(c_int)])
    get_list = bind(dll, "is_GetCameraList", [c_void_p])
    count = c_int()
    rc = get_count(ctypes.byref(count))
    if rc != IS_SUCCESS or count.value <= 0:
        print(f"ERROR: camera enumeration failed: rc={rc}, count={count.value}", file=sys.stderr)
        return 3
    raw = ctypes.create_string_buffer(4 + count.value * ctypes.sizeof(CameraInfo))
    ctypes.cast(raw, ctypes.POINTER(CameraListHead)).contents.dwCount = count.value
    rc = get_list(ctypes.byref(raw))
    if rc != IS_SUCCESS:
        print(f"ERROR: is_GetCameraList returned {rc}", file=sys.stderr)
        return 3
    infos = ctypes.cast(ctypes.addressof(raw) + 4, ctypes.POINTER(CameraInfo))
    cam = next((infos[i] for i in range(count.value) if decode(infos[i].SerNo) == TARGET_SERIAL), None)
    if cam is None:
        print(f"ERROR: SC30 serial {TARGET_SERIAL} not found", file=sys.stderr)
        return 3
    if cam.dwInUse:
        print(f"ERROR: camera {TARGET_SERIAL} is already in use; no changes made", file=sys.stderr)
        return 3

    init = bind(dll, "is_InitCamera", [ctypes.POINTER(c_int), c_void_p])
    exit_cam = bind(dll, "is_ExitCamera", [c_int])
    aoi = bind(dll, "is_AOI", [c_int, c_uint, c_void_p, c_uint])
    binning = bind(dll, "is_SetBinning", [c_int, c_int])
    get_color_depth = bind(dll, "is_GetColorDepth", [c_int, ctypes.POINTER(c_int), ctypes.POINTER(c_int)])
    time_range = bind(dll, "is_GetFrameTimeRange", [c_int, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)])
    exposure = bind(dll, "is_Exposure", [c_int, c_uint, c_void_p, c_uint])

    handle = c_int(int(cam.dwCameraID))
    rc = init(ctypes.byref(handle), None)
    if rc != IS_SUCCESS:
        print(f"ERROR: is_InitCamera returned {rc}; no settings changed", file=sys.stderr)
        return 4

    failures = []

    def query_aoi(command: int, kind, label: str):
        obj = kind()
        result = aoi(handle, command, ctypes.byref(obj), ctypes.sizeof(obj))
        if result != IS_SUCCESS:
            failures.append(f"{label}: rc={result}")
            return None
        if isinstance(obj, Rect):
            return (obj.s32X, obj.s32Y, obj.s32Width, obj.s32Height)
        if isinstance(obj, Size2D):
            return (obj.s32Width, obj.s32Height)
        return (obj.s32X, obj.s32Y)

    try:
        print(f"Camera: serial={TARGET_SERIAL}; camera_id={cam.dwCameraID}; model={decode(cam.Model)}; IDS model label={decode(cam.FullModelName)}; sensor_id={cam.dwSensorID}")
        print("Operation: read-only capability calls; no capture started and no settings written.")

        current_aoi = query_aoi(IS_AOI_IMAGE_GET_AOI, Rect, "current AOI")
        original_aoi = query_aoi(IS_AOI_IMAGE_GET_ORIGINAL_AOI, Rect, "original AOI")
        size_min = query_aoi(IS_AOI_IMAGE_GET_SIZE_MIN, Size2D, "AOI min size")
        size_max = query_aoi(IS_AOI_IMAGE_GET_SIZE_MAX, Size2D, "AOI max size")
        size_inc = query_aoi(IS_AOI_IMAGE_GET_SIZE_INC, Size2D, "AOI size increment")
        pos_min = query_aoi(IS_AOI_IMAGE_GET_POS_MIN, Point2D, "AOI min position")
        pos_max = query_aoi(IS_AOI_IMAGE_GET_POS_MAX, Point2D, "AOI max position")
        pos_inc = query_aoi(IS_AOI_IMAGE_GET_POS_INC, Point2D, "AOI position increment")
        print(f"AOI current (x,y,w,h): {current_aoi}")
        print(f"AOI original (x,y,w,h): {original_aoi}")
        print(f"AOI size: min={size_min}; max={size_max}; increment={size_inc}")
        print(f"AOI position: min={pos_min}; max={pos_max}; increment={pos_inc}")

        supported = binning(handle, IS_GET_SUPPORTED_BINNING)
        current_binning = binning(handle, IS_GET_BINNING)
        horizontal = binning(handle, IS_GET_BINNING_FACTOR_HORIZONTAL)
        vertical = binning(handle, IS_GET_BINNING_FACTOR_VERTICAL)
        mode = binning(handle, IS_GET_BINNING_TYPE)
        print(f"Binning: supported-mask=0x{supported:04X}; current-mask=0x{current_binning:04X}; horizontal-factor={horizontal}; vertical-factor={vertical}; type={mode}")
        print("Binning mask flags: 2x V=0x0001/H=0x0002; 3x V=0x0010/H=0x0020; 4x V=0x0004/H=0x0008; 5x V=0x0040/H=0x0080; 6x V=0x0100/H=0x0200; 8x V=0x0400/H=0x0800; 16x V=0x1000/H=0x2000.")

        bits, color_mode = c_int(), c_int()
        rc = get_color_depth(handle, ctypes.byref(bits), ctypes.byref(color_mode))
        print(f"Current image mode: rc={rc}; bits_per_pixel={bits.value}; color_mode={color_mode.value}")

        tmin, tmax, tinc = ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
        rc = time_range(handle, ctypes.byref(tmin), ctypes.byref(tmax), ctypes.byref(tinc))
        if rc == IS_SUCCESS and tmin.value > 0 and tmax.value > 0:
            print(f"Current-mode frame time: min={tmin.value*1000:.6f} ms ({1/tmin.value:.6f} FPS ceiling); max={tmax.value*1000:.6f} ms ({1/tmax.value:.6f} FPS floor); increment={tinc.value*1000:.6f} ms")
        else:
            failures.append(f"frame time range: rc={rc}")

        exposure_values = []
        for label, command in (("current", IS_EXPOSURE_CMD_GET_EXPOSURE),
                               ("minimum", IS_EXPOSURE_CMD_GET_EXPOSURE_RANGE_MIN),
                               ("maximum", IS_EXPOSURE_CMD_GET_EXPOSURE_RANGE_MAX),
                               ("increment", IS_EXPOSURE_CMD_GET_EXPOSURE_RANGE_INC)):
            value = ctypes.c_double()
            rc = exposure(handle, command, ctypes.byref(value), ctypes.sizeof(value))
            exposure_values.append(f"{label}={'%.6f ms' % value.value if rc == IS_SUCCESS else f'unsupported(rc={rc})'}")
        print("Exposure: " + "; ".join(exposure_values))
    finally:
        rc = exit_cam(handle)
        print(f"is_ExitCamera: {rc}")

    if failures:
        print("Some read-only capability queries were unavailable: " + "; ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
