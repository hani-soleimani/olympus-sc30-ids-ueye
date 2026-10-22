# Live SC30 bead tracking

Run from the olympus-sc30-ids-ueye project root after closing DP2 and any other camera client:

```powershell
.\.venv\Scripts\python.exe tracking\ids_ueye_live_bead_tracking.py
```

The script uses IDS continuous sequence buffers at 2×2 binning, 1024×768, 50 MHz, approximately 10 ms exposure, and the maximum frame rate reported by the driver. It processes only the newest completed frame.

On the first image, draw an ROI around the beads and press Enter or Space. Then click each bead center you want to track and press Enter. Right-click undoes a selection. During tracking, press Q/Esc or close the preview window to stop. The script writes a timestamped annotated MP4 and a CSV of bead positions and visibility under captures/.

Green markers indicate detections. Red markers show a bead temporarily missing from the detector; its last position is held for up to five frames. The CSV includes a row per selected bead per processed frame, with visible or missing status. The displayed FPS measures host processing and display rate, not motion-to-display latency.

The script saves the camera's original binning, AOI, pixel clock, exposure, and configured frame rate, then attempts to restore and report them on normal exit, Q/Esc, or Ctrl+C. Avoid force-killing the process while it owns the camera.
