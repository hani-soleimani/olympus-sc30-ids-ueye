"""Read-only timing diagnostics for the Olympus SC30 through IDS uEye.

This queries live settings and documented capability ranges only. It does not
set parameters or start capture. Close DP2 and all other camera clients first.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import c_char, c_int, c_int32, c_uint, c_uint32, c_void_p

DLL_PATH = r"C:\Windows\System32\uc480_64.dll"
TARGET_SERIAL = "0057004412"
IS_SUCCESS = 0

IS_PIXELCLOCK_CMD_GET_NUMBER = 1
IS_PIXELCLOCK_CMD_GET_LIST = 2
IS_PIXELCLOCK_CMD_GET_RANGE = 3
IS_PIXELCLOCK_CMD_GET_DEFAULT = 4
IS_PIXELCLOCK_CMD_GET = 5
IS_GET_FRAMERATE = 0x8000
IS_GET_EXTERNALTRIGGER = 0x8000
IS_AOI_IMAGE_GET_AOI = 0x0002
IS_GET_BINNING = 0x8000
IS_GET_BINNING_FACTOR_HORIZONTAL = 0x8004
IS_GET_BINNING_FACTOR_VERTICAL = 0x8008
IS_GET_BINNING_TYPE = 0x8002
IS_EXPOSURE_CMD_GET_EXPOSURE = 7


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
    cam = next((infos[i] for i in range(count.value)
                if decode(infos[i].SerNo) == TARGET_SERIAL), None)
    if cam is None:
        print(f"ERROR: SC30 serial {TARGET_SERIAL} not found", file=sys.stderr)
        return 3
    if cam.dwInUse:
        print(f"ERROR: camera {TARGET_SERIAL} is already in use; no changes made", file=sys.stderr)
        return 3

    init = bind(dll, "is_InitCamera", [ctypes.POINTER(c_int), c_void_p])
    exit_cam = bind(dll, "is_ExitCamera", [c_int])
    pixel_clock = bind(dll, "is_PixelClock", [c_int, c_uint, c_void_p, c_uint])
    frame_range = bind(dll, "is_GetFrameTimeRange", [c_int,
        ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double)])
    frame_rate = bind(dll, "is_SetFrameRate", [c_int, ctypes.c_double,
        ctypes.POINTER(ctypes.c_double)])
    get_color_depth = bind(dll, "is_GetColorDepth", [c_int,
        ctypes.POINTER(c_int), ctypes.POINTER(c_int)])
    aoi = bind(dll, "is_AOI", [c_int, c_uint, c_void_p, c_uint])
    binning = bind(dll, "is_SetBinning", [c_int, c_int])
    exposure = bind(dll, "is_Exposure", [c_int, c_uint, c_void_p, c_uint])
    trigger = bind(dll, "is_SetExternalTrigger", [c_int, c_int])

    handle = c_int(int(cam.dwCameraID))
    rc = init(ctypes.byref(handle), None)
    if rc != IS_SUCCESS:
        print(f"ERROR: is_InitCamera returned {rc}; no settings changed", file=sys.stderr)
        return 4

    try:
        print(f"Camera: serial={TARGET_SERIAL}; model={decode(cam.Model)}; IDS label={decode(cam.FullModelName)}; sensor_id={cam.dwSensorID}")
        print("Read-only calls only: no capture started and no settings written.")
        rect = Rect()
        rc = aoi(handle, IS_AOI_IMAGE_GET_AOI, ctypes.byref(rect), ctypes.sizeof(rect))
        print(f"AOI: rc={rc}; x={rect.s32X}, y={rect.s32Y}, width={rect.s32Width}, height={rect.s32Height}")
        print(f"Binning: current-mask=0x{binning(handle, IS_GET_BINNING):04X}; H={binning(handle, IS_GET_BINNING_FACTOR_HORIZONTAL)}; V={binning(handle, IS_GET_BINNING_FACTOR_VERTICAL)}; type={binning(handle, IS_GET_BINNING_TYPE)}")

        for label, command in (("current", IS_PIXELCLOCK_CMD_GET),
                               ("default", IS_PIXELCLOCK_CMD_GET_DEFAULT)):
            value = c_uint()
            rc = pixel_clock(handle, command, ctypes.byref(value), ctypes.sizeof(value))
            print(f"Pixel clock {label}: {'%d MHz' % value.value if rc == IS_SUCCESS else f'unavailable (rc={rc})'}")
        bounds = (c_uint * 3)()
        rc = pixel_clock(handle, IS_PIXELCLOCK_CMD_GET_RANGE, bounds, ctypes.sizeof(bounds))
        if rc == IS_SUCCESS:
            print(f"Pixel clock range: min={bounds[0]} MHz, max={bounds[1]} MHz, increment={bounds[2]} MHz (increment 0 means discrete values)")
        else:
            print(f"Pixel clock range: unavailable (rc={rc})")
        n = c_uint()
        rc = pixel_clock(handle, IS_PIXELCLOCK_CMD_GET_NUMBER, ctypes.byref(n), ctypes.sizeof(n))
        if rc == IS_SUCCESS and 0 < n.value <= 150:
            values = (c_uint * n.value)()
            rc = pixel_clock(handle, IS_PIXELCLOCK_CMD_GET_LIST, values, ctypes.sizeof(values))
            print(f"Supported pixel clocks: {list(values) if rc == IS_SUCCESS else f'unavailable (rc={rc})'}")
        elif rc == IS_SUCCESS:
            print(f"Supported discrete pixel-clock count: {n.value}")
        else:
            print(f"Supported pixel clocks: unavailable (rc={rc})")

        tmin, tmax, tinc = ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
        rc = frame_range(handle, ctypes.byref(tmin), ctypes.byref(tmax), ctypes.byref(tinc))
        if rc == IS_SUCCESS and tmin.value > 0:
            print(f"Frame-time range: min={tmin.value*1000:.6f} ms ({1/tmin.value:.6f} FPS ceiling); max={tmax.value*1000:.6f} ms ({1/tmax.value:.6f} FPS floor); increment={tinc.value*1000:.6f} ms")
        else:
            print(f"Frame-time range: unavailable (rc={rc})")
        fps = ctypes.c_double()
        rc = frame_rate(handle, ctypes.c_double(IS_GET_FRAMERATE), ctypes.byref(fps))
        print(f"Configured frame rate: {'%.6f FPS' % fps.value if rc == IS_SUCCESS else f'unavailable (rc={rc})'}")
        bits, color_mode = c_int(), c_int()
        rc = get_color_depth(handle, ctypes.byref(bits), ctypes.byref(color_mode))
        print(f"Color format: rc={rc}; bits_per_pixel={bits.value}; color_mode={color_mode.value}")
        value = ctypes.c_double()
        rc = exposure(handle, IS_EXPOSURE_CMD_GET_EXPOSURE,
                      ctypes.byref(value), ctypes.sizeof(value))
        print(f"Exposure: {'%.6f ms' % value.value if rc == IS_SUCCESS else f'unavailable (rc={rc})'}")
        trig = trigger(handle, IS_GET_EXTERNALTRIGGER)
        print(f"External trigger mode query: {trig} (IDS readback; 0 is trigger-off/free-run)")
    finally:
        print(f"is_ExitCamera: {exit_cam(handle)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
