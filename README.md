# Olympus SC30 IDS uEye

A standalone Windows/Python project for acquiring Olympus SC30 camera frames through the installed IDS uEye API. It is separate from the Olympus TWAIN connector and contains no imports from that project.

## Camera and driver

The camera is an Olympus SC30 connected through the Olympus/IDS OLYUSB3 driver. The working Python acquisition experiments use IDS uEye calls exported by the installed `uc480_64.dll` (observed on the test PC at `C:\Windows\System32\uc480_64.dll`, IDS/OEM driver suite 4.93.2111). Install the IDS uEye driver/software suite that recognizes the SC30 before using these scripts. Do not copy vendor DLLs into this repository; the driver installer supplies them.

The scripts select the camera by serial number and check whether IDS reports it in use. Close DP2, any TWAIN source window, and any other IDS preview first. Only one process should own the camera.

## Python setup

Use 64-bit Python 3.11 or newer on Windows. From this project directory:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

OpenCV and NumPy are required by the graphical preview. The diagnostic and acquisition experiments otherwise use the Python standard library and `ctypes`.

## Run the 50 MHz live preview

Close DP2 and other camera clients, then run from the repository root:

```powershell
.\.venv\Scripts\python.exe camera\ids_ueye_live_preview_50mhz.py
```

The preview requests 2×2 binning (1024×768 output), 50 MHz pixel clock, approximately 10 ms exposure, and the fastest frame rate reported by the driver. It continuously acquires into an IDS sequence buffer ring and displays only the newest completed frame. The image window overlays rolling/displayed FPS, driver-reported FPS, and the displayed-frame interval. Press **Q**, **Esc**, or close the preview window to stop. The terminal prints the final timing summary.

The script saves the original binning, AOI, pixel clock, exposure, and configured frame rate before changing them. It stops acquisition, clears the sequence, frees buffers, restores those settings, verifies the readbacks, and closes the camera handle on normal exit. Avoid force-killing Python or disconnecting the camera during a run because that bypasses Python cleanup.

## Measured performance

An 8-second continuous-acquisition test at 2×2 binning and 10 ms exposure produced these results:

| Pixel clock | Driver timing limit | Measured buffer-arrival rate | Median / p95 interval |
|---:|---:|---:|---:|
| 24 MHz | 17.19 FPS | 16.59 FPS | 58.23 / 66.44 ms |
| 50 MHz | 35.81 FPS | 35.60 FPS | 27.95 / 32.91 ms |

The buffers were 1024×768, 32 bits per pixel, color mode 0, with a 4096-byte row pitch. The pixel-clock test observed zero skipped sequence-buffer slots and zero `is_GetActSeqBuf` errors. One long interval occurred in each run; the longest was 68.75 ms at 50 MHz. These are measurements from continuous acquisition, not proof of zero sensor or USB frame loss.

The acquisition rate, displayed rate, and motion-to-display latency describe different things:

- **Acquisition FPS** is how quickly new camera frames arrive in the IDS sequence buffers. The test measured about 35.6 FPS at 50 MHz.
- **Displayed FPS** is how quickly this Python/OpenCV preview copies, converts, and submits frames to its window. It can be lower than acquisition FPS because of copying, color conversion, window scheduling, and display refresh. The preview reports this while running; it has not yet been independently measured on the desktop.
- **Motion-to-display latency** is the time from a physical bead moving to that change appearing on screen. Frame-rate measurements alone do not determine it; this requires a timed motion/visual test or a high-speed recording of both the event and display.

## Experiments

All experiments are standalone scripts and do not import the TWAIN connector:

- `experiments/ids_ueye_pixelclock_sweep.py` compares measured acquisition at 24 MHz and the highest pixel clock supported in 2×2 mode, then restores settings.
- `experiments/ids_ueye_binning2_rate_test.py` measures delivered FPS and intervals at 2×2 binning with the driver timing limit.
- `experiments/ids_ueye_timing_diagnostics.py` queries current timing settings and ranges without starting capture or writing settings.
- `experiments/ids_ueye_read_capabilities.py` queries AOI, binning, color-depth, exposure, and timing capabilities without setting values.
- `experiments/ids_ueye_live_probe.py` tests continuous acquisition using current settings without changing them.
- `experiments/ids_ueye_live_preview_17fps.py` preserves the earlier 2×2 low-rate preview for comparison.

For example, close all camera clients and run a reversible rate test with:

```powershell
.\.venv\Scripts\python.exe experiments\ids_ueye_pixelclock_sweep.py --seconds 8
```

## Project layout

```text
camera/       Interactive 50 MHz SC30 preview
experiments/  Capability queries and acquisition benchmarks
tracking/     Reserved for future bead/particle tracking code
```

## Safety and troubleshooting

- “Camera is in use” means another process owns the device; close DP2, TWAIN, and IDS applications before retrying.
- The scripts abort before changing settings if the camera is already in use or if required modes are unavailable.
- Camera changes are kept in separate experiments. The 50 MHz preview is its own file and the previous 17 FPS preview was copied unchanged for comparison.
- `uc480_64.dll` and the camera driver must match the installed 64-bit IDS software. Do not add DLL binaries or driver files to Git.
- A preview FPS label reports host display submissions; it is not a motion-to-display latency measurement.
