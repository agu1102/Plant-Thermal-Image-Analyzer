"""
Plant Thermal Image Analyzer
Version 1.0

Developed by Kathy Yu Xuan Gu
Schroeder Laboratory
University of California San Diego

Features
--------
- Radiometric FLIR JPG analysis without PlantCV
- Any positive number of plants
- Optional custom plant names editable BEFORE analysis
- Manual polygon plant masks
- Optional soil/background exclusion
- Safe cancel confirmation from selection windows
- QC mask overlay
- Mean, 2% trimmed mean, median, min, max, pixel count
- CSV and PNG outputs
- Final summary with optional Edit Plant Names
"""

from datetime import datetime
from pathlib import Path
import csv
import os
import re
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Figures are only written to disk, never shown on screen. Forcing the
# non-interactive Agg backend before Matplotlib is ever imported keeps it
# from starting a second GUI toolkit, which deadlocks Tk on macOS.
os.environ.setdefault("MPLBACKEND", "Agg")

BUNDLE_ID = "edu.ucsd.schroederlab.plantthermalanalyzer"


def bundle_dir():
    """Directory holding bundled read-only resources.

    PyInstaller sets sys._MEIPASS in both one-file and one-folder builds.
    When running from source this is simply the script's own directory.
    """
    packed = getattr(sys, "_MEIPASS", None)
    if packed:
        return Path(packed)
    return Path(__file__).resolve().parent


def prepare_matplotlib_cache():
    """Give Matplotlib a persistent, pre-seeded font cache.

    Two separate problems are fixed here, and both must be handled before
    anything imports Matplotlib - which flirimageextractor does at import
    time, so this runs before that import rather than inside a function.

    1. PyInstaller's Matplotlib runtime hook points MPLCONFIGDIR at a fresh
       temporary directory on every launch and deletes it on exit. That guards
       one-file builds, whose extraction folder changes between runs, but this
       is a one-folder .app at a stable path. The only effect is that
       Matplotlib rebuilds its font cache at every startup: roughly 35 seconds
       of apparent hang before the window appears.

    2. Even with a persistent directory, the very first launch on each machine
       pays that cost once. The build seeds a prebuilt cache into the bundle,
       which is copied into place here.

    Every failure is non-fatal: the worst case is Matplotlib rebuilding the
    cache itself, which is exactly the old behaviour.
    """
    if not (sys.platform == "darwin" and getattr(sys, "frozen", False)):
        return

    cache_dir = Path.home() / "Library" / "Caches" / BUNDLE_ID / "matplotlib"

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    os.environ["MPLCONFIGDIR"] = str(cache_dir)

    seed_dir = bundle_dir() / "vendor" / "mpl-fontcache"

    if not seed_dir.is_dir():
        return

    for seed in seed_dir.glob("fontlist-*.json"):
        target = cache_dir / seed.name
        if target.exists():
            continue
        try:
            shutil.copyfile(seed, target)
        except OSError:
            pass


prepare_matplotlib_cache()

import cv2
import numpy as np
from flirimageextractor import FlirImageExtractor


IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")

# Segoe UI does not exist on macOS; Tk would silently fall back to a
# noticeably worse font.
if IS_MAC:
    UI_FONT = "Helvetica Neue"
elif IS_WINDOWS:
    UI_FONT = "Segoe UI"
else:
    UI_FONT = "DejaVu Sans"


def find_exiftool():
    """Locate the ExifTool executable used to read FLIR radiometric data.

    Frozen builds should prefer the copy bundled with the application so
    end users do not need to install ExifTool separately. Source/development
    runs may fall back to ExifTool available on PATH.
    """
    resource_root = bundle_dir() / "vendor" / "exiftool"

    if IS_WINDOWS:
        candidates = [
            resource_root / "exiftool.exe",
            resource_root / "exiftool(-k).exe",
        ]
    else:
        candidates = [
            resource_root / "exiftool",
        ]

    for bundled in candidates:
        if not bundled.is_file():
            continue

        if not IS_WINDOWS and not os.access(bundled, os.X_OK):
            try:
                bundled.chmod(0o755)
            except OSError:
                pass

        if IS_WINDOWS or os.access(bundled, os.X_OK):
            return str(bundled)

    found = shutil.which("exiftool")
    if found:
        return found

    return "exiftool.exe" if IS_WINDOWS else "exiftool"


EXIFTOOL_PATH = find_exiftool()


def clean_subprocess_env():
    """Undo loader variables PyInstaller may inject before running helpers.

    ExifTool runs through the system Perl. If the frozen application's own
    library paths leaked into its environment it could pick up the wrong
    shared libraries.
    """
    if not getattr(sys, "frozen", False):
        return

    for name in ("DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"):
        original = os.environ.pop(f"{name}_ORIG", None)
        if original is not None:
            os.environ[name] = original
        else:
            os.environ.pop(name, None)


def exiftool_available():
    try:
        subprocess.run(
            [EXIFTOOL_PATH, "-ver"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=True,
        )
        return True
    except Exception:
        return False


APP_NAME = "Plant Thermal Image Analyzer"
VERSION = "1.0"
AUTHOR = "Kathy Yu Xuan Gu"
LAB = "Schroeder Laboratory"
INSTITUTION = "University of California San Diego"


class AnalysisCancelled(Exception):
    pass


class NotRadiometricError(ValueError):
    """The file carries no embedded FLIR temperature data."""


# Calibration values without which no temperature can be computed. Their
# presence is what separates a radiometric FLIR JPG from a picture of one.
REQUIRED_FLIR_TAGS = ("Planck R1", "Planck B", "Planck O", "Planck R2")

# Names as ExifTool prints them, mapped to parse_flir keyword arguments.
# Plain numbers.
FLIR_NUMERIC_TAGS = (
    ("emissivity", "Emissivity"),
    ("ir_window_transmission", "IR Window Transmission"),
    ("planck_r1", "Planck R1"),
    ("planck_b", "Planck B"),
    ("planck_f", "Planck F"),
    ("planck_o", "Planck O"),
    ("planck_r2", "Planck R2"),
    ("ata1", "Atmospheric Trans Alpha 1"),
    ("ata2", "Atmospheric Trans Alpha 2"),
    ("atb1", "Atmospheric Trans Beta 1"),
    ("atb2", "Atmospheric Trans Beta 2"),
    ("atx", "Atmospheric Trans X"),
)

# Values printed with a trailing unit, such as "1.5 m" or "22.0 C".
FLIR_UNIT_TAGS = (
    ("object_distance", "Object Distance"),
    ("atmospheric_temperature", "Atmospheric Temperature"),
    ("reflected_apparent_temperature", "Reflected Apparent Temperature"),
    ("ir_window_temperature", "IR Window Temperature"),
    ("relative_humidity", "Relative Humidity"),
)


def read_flir_metadata(path):
    """Return ExifTool's tag dump as a dict, the way flirimageextractor does."""
    dump = subprocess.run(
        [EXIFTOOL_PATH, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    ).stdout.decode("utf8", "replace").replace("\r", "")

    metadata = {}
    for line in dump.split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            metadata[key.strip()] = value.strip()

    return metadata


def read_thermal_without_dji_sdk(path):
    """Decode a FLIR radiometric JPG without loading the DJI Thermal SDK.

    flirimageextractor.Thermal.__init__ loads DJI Thermal SDK shared
    libraries, and DJI only ships those for Windows and Linux, so on macOS it
    raises before any image is read. The decoder actually used for FLIR
    cameras, Thermal.parse_flir, is pure Python and needs nothing but
    ExifTool, so the object is created without running __init__ and given the
    two attributes that this code path reads.

    Thermal.parse is deliberately not reused. It asserts on a `Camera Model
    Name` tag and then checks that model against a short hard-coded list, so
    it rejects FLIR cameras whose model string does not happen to contain
    "FLIR" - a T1020 or an A655sc, for instance. The calibration constants are
    what actually decide whether an image can be converted to temperature, so
    those are what we check. The arithmetic is still the library's own
    unmodified parse_flir.

    DJI drone R-JPEGs genuinely require the SDK and are not supported here.
    """
    # macOS-only dependency. Import lazily so Windows does not need this
    # submodule merely to start the application.
    try:
        from flirimageextractor.thermal import Thermal
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "The installed flirimageextractor package does not provide "
            "flirimageextractor.thermal, which is required for the macOS "
            "FLIR decoder in this build."
        ) from exc

    metadata = read_flir_metadata(path)

    missing = [tag for tag in REQUIRED_FLIR_TAGS if tag not in metadata]
    if missing:
        raise NotRadiometricError(", ".join(missing))

    kwargs = {}

    for name, tag in FLIR_NUMERIC_TAGS:
        if tag in metadata:
            try:
                kwargs[name] = float(metadata[tag])
            except ValueError:
                pass

    for name, tag in FLIR_UNIT_TAGS:
        if tag in metadata:
            # "1.5 m", "22.0 C", "50.0 %" - drop the trailing unit.
            try:
                kwargs[name] = float(metadata[tag][:-2])
            except ValueError:
                pass

    thermal = Thermal.__new__(Thermal)
    thermal._dtype = np.float32
    thermal._filepath_exiftool = EXIFTOOL_PATH

    return thermal.parse_flir(filepath_image=str(path), **kwargs)


def get_thermal(path):
    """
    Read radiometric temperature data from a FLIR JPG.

    Ordinary JPG files may look like thermal images but do not necessarily
    contain the embedded radiometric FLIR data required for quantitative
    temperature analysis.
    """
    try:
        if IS_MAC:
            raw = read_thermal_without_dji_sdk(path)
        else:
            flir = FlirImageExtractor(exiftool_path=EXIFTOOL_PATH)
            flir.process_image(flir_img_file=str(path))
            raw = flir.get_thermal_np()

        if raw is None:
            raise ValueError

        data = np.asarray(raw, dtype=np.float32)

        if data.ndim != 2 or data.size == 0:
            raise ValueError

        if not np.isfinite(data).any():
            raise ValueError

        return data

    except NotRadiometricError as exc:
        raise ValueError(
            "This image does not contain embedded radiometric FLIR thermal "
            "data.\n\n"
            f"Missing calibration data: {exc}\n\n"
            "Please select an original radiometric FLIR JPG, straight off the "
            "camera. A screenshot, an exported or re-saved thermal picture, "
            "or an image that has been emailed or messaged can look identical "
            "but has had the temperature data stripped out of it."
        ) from exc

    except Exception as exc:
        # Something other than missing calibration data went wrong. Say so,
        # and keep the detail: blaming the image would send the user looking
        # in the wrong place.
        raise ValueError(
            "The radiometric FLIR data in this image could not be read.\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            "The calibration tags are present, so this is a decoding problem "
            "rather than an unsuitable image. Please report this message "
            "together with the image."
        ) from exc


def color_image(data):
    lo = float(np.nanmin(data))
    hi = float(np.nanmax(data))
    if hi <= lo:
        raise ValueError("Invalid thermal range.")
    gray = np.clip((data - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)
    return cv2.applyColorMap(gray, cv2.COLORMAP_JET)


def polygon_mask(shape, points):
    mask = np.zeros(shape, dtype=np.uint8)
    cv2.fillPoly(mask, [points], 255)
    return mask


def plant_stats(data, mask):
    values = np.sort(data[mask > 0].astype(float))
    if values.size == 0:
        raise ValueError("Empty plant mask.")

    k = int(values.size * 0.02)
    trimmed = values[k:-k] if k > 0 and values.size > 2 * k else values

    return {
        "mean": float(values.mean()),
        "trimmed": float(trimmed.mean()),
        "median": float(np.median(values)),
        "min": float(values.min()),
        "max": float(values.max()),
        "pixels": int(values.size),
    }


def safe_filename(text):
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", text.strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._")
    return cleaned or "plant"


def bring_cv_window_forward(title):
    """Pull an OpenCV window in front of the Tk windows.

    On macOS a newly created OpenCV window frequently opens behind the Tk
    window that spawned it, which looks like the application has hung.
    """
    if not IS_MAC:
        return

    try:
        cv2.setWindowProperty(title, cv2.WND_PROP_TOPMOST, 1)
    except cv2.error:
        # Purely cosmetic; never let focus handling break the analysis.
        pass


# Every step of the analysis draws into this one OpenCV window. Creating and
# destroying a window per plant made windows flash in and out, and on macOS
# each new window tended to open behind the others.
WORKFLOW_WINDOW = "Plant Thermal Image Analyzer - Analysis"

# Height of the instruction strip above the image. It is extra canvas rather
# than an overlay, so it never covers selectable thermal pixels.
HEADER_H = 82

FONT = cv2.FONT_HERSHEY_SIMPLEX

# Last canvas shown, so a confirmation can be drawn on top of it.
_workflow_state = {"canvas": None}


def display_size(base_width, width, height):
    """Screen size for the image area, capped at 1200x800."""
    target_w = max(640, min(1200, base_width * 2))
    target_h = int(round(target_w * height / width))

    if target_h > 800:
        target_h = 800
        target_w = int(round(target_h * width / height))

    return max(1, target_w), max(1, target_h)


def draw_header(canvas, title, hint_lines, right_text=None):
    """Draw the instruction strip. Text must be ASCII: OpenCV cannot do more."""
    cv2.putText(canvas, title, (14, 25), FONT, 0.62, (25, 25, 25), 1,
                cv2.LINE_AA)

    if right_text:
        (text_w, _), _ = cv2.getTextSize(right_text, FONT, 0.52, 1)
        cv2.putText(canvas, right_text,
                    (max(14, canvas.shape[1] - text_w - 16), 25),
                    FONT, 0.52, (45, 45, 45), 1, cv2.LINE_AA)

    for index, line in enumerate(hint_lines[:2]):
        cv2.putText(canvas, line, (14, 51 + index * 21), FONT, 0.45,
                    (55, 55, 55), 1, cv2.LINE_AA)


def workflow_window_open():
    try:
        return cv2.getWindowProperty(
            WORKFLOW_WINDOW, cv2.WND_PROP_VISIBLE
        ) >= 1
    except cv2.error:
        return False


def ensure_workflow_window(reset_size=False):
    """Create the analysis window if it is not already on screen."""
    if workflow_window_open() and not reset_size:
        return

    cv2.namedWindow(WORKFLOW_WINDOW, cv2.WINDOW_NORMAL)

    if reset_size:
        try:
            cv2.resizeWindow(WORKFLOW_WINDOW, 1000, 800)
            cv2.moveWindow(WORKFLOW_WINDOW, 60, 60)
        except cv2.error:
            pass

    bring_cv_window_forward(WORKFLOW_WINDOW)


def show_workflow(canvas):
    _workflow_state["canvas"] = canvas
    cv2.imshow(WORKFLOW_WINDOW, canvas)


def close_workflow_window():
    _workflow_state["canvas"] = None
    try:
        cv2.destroyWindow(WORKFLOW_WINDOW)
        cv2.waitKey(1)
    except cv2.error:
        pass


def confirm_in_window(question, detail):
    """Ask a yes/no question inside the analysis window.

    A Tk dialog here would be a third window competing for focus with the
    OpenCV window behind it, which on macOS regularly came up behind or
    unresponsive. Drawing the question onto the image keeps the whole
    selection workflow in one place.
    """
    ensure_workflow_window()

    canvas = _workflow_state["canvas"]

    if canvas is None:
        canvas = np.full((HEADER_H + 300, 900, 3), 242, dtype=np.uint8)

    view = canvas.copy()
    height, width = view.shape[:2]

    # Dim everything, then float a panel in the middle.
    view = (view.astype(np.float32) * 0.35).astype(np.uint8)

    panel_w = min(width - 60, 720)
    panel_h = 132 + 24 * len(detail)
    x0 = (width - panel_w) // 2
    y0 = (height - panel_h) // 2

    cv2.rectangle(view, (x0, y0), (x0 + panel_w, y0 + panel_h),
                  (250, 250, 250), -1)
    cv2.rectangle(view, (x0, y0), (x0 + panel_w, y0 + panel_h),
                  (90, 90, 90), 1)

    cv2.putText(view, question, (x0 + 26, y0 + 48), FONT, 0.66,
                (20, 20, 20), 2, cv2.LINE_AA)

    for index, line in enumerate(detail):
        cv2.putText(view, line, (x0 + 26, y0 + 84 + index * 24), FONT, 0.48,
                    (60, 60, 60), 1, cv2.LINE_AA)

    cv2.putText(view, "Y: Yes        N: No", (x0 + 26, y0 + panel_h - 26),
                FONT, 0.58, (20, 20, 20), 2, cv2.LINE_AA)

    cv2.imshow(WORKFLOW_WINDOW, view)

    while True:
        if not workflow_window_open():
            # The window was closed outright; take that as a yes.
            return True

        key = cv2.waitKeyEx(20)

        if key in (ord("y"), ord("Y")):
            return True

        if key in (ord("n"), ord("N"), 27):
            if _workflow_state["canvas"] is not None:
                cv2.imshow(WORKFLOW_WINDOW, _workflow_state["canvas"])
            return False


def fit_window(window, width, height):
    """Size a window to at least `width` x `height`, growing to fit content.

    The layout was tuned against Segoe UI on Windows. macOS system fonts are
    wider and taller, so fixed pixel geometries clip buttons. Asking Tk for
    the real requested size avoids hard-coding per-platform numbers.
    """
    window.update_idletasks()

    width = max(width, window.winfo_reqwidth())
    height = max(height, window.winfo_reqheight())

    window.geometry(f"{width}x{height}")


def ascii_window_label(text):
    """OpenCV window titles on Windows are safest with ASCII-only text."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "Custom label"


class PolygonSelector:
    """
    Cross-platform polygon selector with keyboard-only view navigation.

    Controls
    --------
    Left click       Add polygon point
    Z                Undo last point
    R                Redraw / reset polygon
    +                Zoom in
    -                Zoom out
    Arrow keys       Move the current view
    0                Fit / reset full image view
    Enter / Return   Finish selection (3+ points)
    Esc / window X   Request cancellation

    Important:
    - The mouse is used ONLY for adding points.
    - Zoom and movement affect only the displayed view.
    - Polygon points are always stored in ORIGINAL image coordinates.
    """

    def __init__(self, image, title, reset_window_size=False):
        self.base = image.copy()
        self.title = title
        self.points = []
        self.reset_window_size = reset_window_size

        self.img_h, self.img_w = self.base.shape[:2]

        # Current view rectangle in original-image coordinates.
        self.view_x = 0.0
        self.view_y = 0.0
        self.view_w = float(self.img_w)
        self.view_h = float(self.img_h)

        self.display_w = self.img_w
        self.display_h = self.img_h

        # Maximum zoom is about 30x relative to the original full view.
        self.min_view_w = max(20.0, self.img_w * 0.03)
        self.min_view_h = max(20.0, self.img_h * 0.03)

    def clamp_view(self):
        self.view_w = min(
            max(self.view_w, self.min_view_w),
            float(self.img_w),
        )
        self.view_h = min(
            max(self.view_h, self.min_view_h),
            float(self.img_h),
        )

        self.view_x = min(
            max(self.view_x, 0.0),
            max(0.0, self.img_w - self.view_w),
        )
        self.view_y = min(
            max(self.view_y, 0.0),
            max(0.0, self.img_h - self.view_h),
        )

    def reset_view(self):
        self.view_x = 0.0
        self.view_y = 0.0
        self.view_w = float(self.img_w)
        self.view_h = float(self.img_h)
        self.refresh()

    def screen_to_image(self, sx, sy):
        ix = self.view_x + (
            float(sx) / max(1, self.display_w)
        ) * self.view_w

        iy = self.view_y + (
            float(sy) / max(1, self.display_h)
        ) * self.view_h

        ix = int(round(min(max(ix, 0), self.img_w - 1)))
        iy = int(round(min(max(iy, 0), self.img_h - 1)))

        return ix, iy

    def image_to_screen(self, ix, iy):
        sx = (
            (float(ix) - self.view_x)
            / self.view_w
            * self.display_w
        )

        sy = (
            (float(iy) - self.view_y)
            / self.view_h
            * self.display_h
        )

        return int(round(sx)), int(round(sy))

    def zoom(self, factor):
        """
        Zoom around the center of the current view.
        factor < 1 = zoom in
        factor > 1 = zoom out
        """
        center_x = self.view_x + self.view_w / 2.0
        center_y = self.view_y + self.view_h / 2.0

        new_w = self.view_w * factor
        new_h = self.view_h * factor

        new_w = min(
            max(new_w, self.min_view_w),
            float(self.img_w),
        )
        new_h = min(
            max(new_h, self.min_view_h),
            float(self.img_h),
        )

        self.view_w = new_w
        self.view_h = new_h
        self.view_x = center_x - new_w / 2.0
        self.view_y = center_y - new_h / 2.0

        self.clamp_view()
        self.refresh()

    def move_view(self, direction):
        """
        Move by 10% of the CURRENT visible area.
        This gives coarse movement when zoomed out and finer movement
        automatically when zoomed in.
        """
        step_x = self.view_w * 0.10
        step_y = self.view_h * 0.10

        if direction == "left":
            self.view_x -= step_x
        elif direction == "right":
            self.view_x += step_x
        elif direction == "up":
            self.view_y -= step_y
        elif direction == "down":
            self.view_y += step_y

        self.clamp_view()
        self.refresh()

    def refresh(self):
        x1 = int(round(self.view_x))
        y1 = int(round(self.view_y))
        x2 = int(round(self.view_x + self.view_w))
        y2 = int(round(self.view_y + self.view_h))

        x1 = max(0, min(x1, self.img_w - 1))
        y1 = max(0, min(y1, self.img_h - 1))
        x2 = max(x1 + 1, min(x2, self.img_w))
        y2 = max(y1 + 1, min(y2, self.img_h))

        crop = self.base[y1:y2, x1:x2].copy()

        target_w = max(640, min(1200, self.img_w * 2))
        target_h = int(round(target_w * crop.shape[0] / crop.shape[1]))

        if target_h > 800:
            target_h = 800
            target_w = int(round(target_h * crop.shape[1] / crop.shape[0]))

        self.display_w = max(1, target_w)
        self.display_h = max(1, target_h)

        # Display-only smoothing:
        # INTER_CUBIC makes the enlarged thermal view look less blocky.
        # This does NOT change the original radiometric matrix, polygon
        # coordinates, masks, or temperature calculations.
        thermal_view = cv2.resize(
            crop,
            (self.display_w, self.display_h),
            interpolation=cv2.INTER_CUBIC,
        )

        # Draw selected points ONLY on the thermal-image portion.
        screen_points = []
        for point in self.points:
            sx, sy = self.image_to_screen(*point)
            screen_points.append((sx, sy))

            if 0 <= sx < self.display_w and 0 <= sy < self.display_h:
                cv2.circle(
                    thermal_view, (sx, sy), 4,
                    (255, 255, 255), -1
                )

        if len(screen_points) > 1:
            cv2.polylines(
                thermal_view,
                [np.array(screen_points, dtype=np.int32)],
                False,
                (255, 255, 255),
                2,
            )

        zoom_percent = int(round(self.img_w / self.view_w * 100))

        # Dedicated UI header. This is EXTRA canvas space and therefore
        # does not cover any selectable thermal-image pixels.
        canvas = np.full(
            (HEADER_H + self.display_h, self.display_w, 3),
            242,
            dtype=np.uint8,
        )
        canvas[HEADER_H:, :] = thermal_view

        draw_header(
            canvas,
            self.title,
            [
                "Click: Add point    Z: Undo    R: Redraw    "
                "+/-: Zoom    Arrow keys: Move",
                "0: Fit image    Enter: Finish plant    Esc: Cancel analysis",
            ],
            right_text=f"Zoom: {zoom_percent}%",
        )

        show_workflow(canvas)

    def mouse(self, event, x, y, flags, param):
        # Mouse input is intentionally simple:
        # LEFT CLICK ONLY adds a point. No drag/pan state exists.
        if event == cv2.EVENT_LBUTTONDOWN:
            # Ignore clicks in the instruction/header area.
            if y < HEADER_H:
                return

            image_y = y - HEADER_H

            if 0 <= x < self.display_w and 0 <= image_y < self.display_h:
                self.points.append(
                    self.screen_to_image(x, image_y)
                )
                self.refresh()

    def run(self):
        ensure_workflow_window(reset_size=self.reset_window_size)
        cv2.setMouseCallback(WORKFLOW_WINDOW, self.mouse)

        self.refresh()
        cancel_requested = False

        try:
            while True:
                if not workflow_window_open():
                    cancel_requested = True
                    break

                key = cv2.waitKeyEx(20)

                if key == -1:
                    continue

                # Enter / Return
                if key in (13, 10):
                    if len(self.points) >= 3:
                        break
                    continue

                # Escape
                if key == 27:
                    cancel_requested = True
                    break

                # Undo
                if key in (ord("z"), ord("Z")):
                    if self.points:
                        self.points.pop()
                        self.refresh()
                    continue

                # Redraw / reset polygon, but KEEP zoom and current view.
                if key in (ord("r"), ord("R")):
                    self.points = []
                    self.refresh()
                    continue

                # Zoom
                if key in (ord("+"), ord("=")):
                    self.zoom(0.80)
                    continue

                if key in (ord("-"), ord("_")):
                    self.zoom(1.25)
                    continue

                # Fit image
                if key == ord("0"):
                    self.reset_view()
                    continue

                # Arrow key codes differ slightly across OpenCV/platform builds.
                # Include common Windows/Linux/macOS values.
                left_codes = {
                    2424832,   # Windows
                    65361,     # X11/Linux
                    81,        # some OpenCV builds
                    63234,     # macOS Cocoa (NSLeftArrowFunctionKey)
                }
                up_codes = {
                    2490368,
                    65362,
                    82,
                    63232,     # macOS Cocoa
                }
                right_codes = {
                    2555904,
                    65363,
                    83,
                    63235,     # macOS Cocoa
                }
                down_codes = {
                    2621440,
                    65364,
                    84,
                    63233,     # macOS Cocoa
                }

                if key in left_codes:
                    self.move_view("left")
                    continue

                if key in up_codes:
                    self.move_view("up")
                    continue

                if key in right_codes:
                    self.move_view("right")
                    continue

                if key in down_codes:
                    self.move_view("down")
                    continue

        finally:
            # The window stays up for the next step of the workflow. Only the
            # mouse handler is detached, so stray clicks cannot add points to
            # a polygon that is already finished.
            if workflow_window_open():
                try:
                    cv2.setMouseCallback(
                        WORKFLOW_WINDOW, lambda *args: None
                    )
                except cv2.error:
                    pass

        if cancel_requested:
            return "CANCEL_REQUEST"

        return np.array(
            self.points,
            dtype=np.int32,
        )


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("760x650")
        self.root.minsize(700, 520)

        self.image = tk.StringVar()
        self.output = tk.StringVar()
        self.count = tk.StringVar(value="")
        self.use_custom_names = tk.BooleanVar(value=False)
        self.status = tk.StringVar(
            value="Select a radiometric FLIR JPG to begin."
        )
        self.image_status = tk.StringVar(value="")

        self.name_vars = []
        self.saved_name_values = []

        # Current completed session, used by "Edit Plant Names".
        self.session = None
        self.reset_selection_window_next = True

        # Cache a successfully validated FLIR image so Start Analysis does
        # not need to extract the radiometric matrix a second time.
        self.validated_image_path = None
        self.validated_thermal = None

        self.build_menu()
        self.build_ui()

        fit_window(self.root, 760, 650)

        self.count.trace_add("write", self.on_setup_change)

    def build_menu(self):
        menubar = tk.Menu(self.root)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menubar)

        if IS_MAC:
            # Route the standard macOS "About <App>" item to the same dialog.
            try:
                self.root.createcommand("tkAboutDialog", self.show_about)
            except tk.TclError:
                pass

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=25)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        ttk.Label(
            outer,
            text=APP_NAME,
            font=(UI_FONT, 19, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(
            outer,
            text=(
                "Quantitative analysis of radiometric FLIR images "
                "using user-defined plant regions."
            ),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 20))

        ttk.Label(
            outer, text="Radiometric FLIR image", font=(UI_FONT, 10, "bold")
        ).grid(row=2, column=0, sticky="w")

        ttk.Entry(
            outer, textvariable=self.image
        ).grid(row=3, column=0, sticky="ew", padx=(0, 8))

        ttk.Button(
            outer, text="Browse...", command=self.choose_image
        ).grid(row=3, column=1)

        ttk.Label(
            outer,
            textvariable=self.image_status,
            foreground="#666666",
            wraplength=670,
        ).grid(
            row=4,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(4, 0),
        )

        ttk.Label(
            outer, text="Number of plants", font=(UI_FONT, 10, "bold")
        ).grid(row=5, column=0, sticky="w", pady=(16, 4))

        plant_row = ttk.Frame(outer)
        plant_row.grid(row=6, column=0, columnspan=2, sticky="w")

        ttk.Entry(
            plant_row, textvariable=self.count, width=10
        ).pack(side="left")

        ttk.Label(
            plant_row, text="Enter any positive whole number."
        ).pack(side="left", padx=(10, 0))

        ttk.Checkbutton(
            outer,
            text="Add custom plant names / experimental labels",
            variable=self.use_custom_names,
            command=self.toggle_custom_names,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(14, 0))

        ttk.Label(
            outer,
            text="Optional — examples: Control_1, ABA_1, WT_Control, ost1_ABA.",
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(2, 5))

        self.names_container = ttk.LabelFrame(
            outer,
            text="Plant names / experimental labels",
            padding=10,
        )
        self.names_container.grid(
            row=9, column=0, columnspan=2, sticky="nsew", pady=(4, 4)
        )
        self.names_container.grid_remove()

        self.names_canvas = tk.Canvas(
            self.names_container,
            height=155,
            highlightthickness=0,
        )
        self.names_scrollbar = ttk.Scrollbar(
            self.names_container,
            orient="vertical",
            command=self.names_canvas.yview,
        )
        self.names_inner = ttk.Frame(self.names_canvas)

        self.names_inner.bind(
            "<Configure>",
            lambda e: self.names_canvas.configure(
                scrollregion=self.names_canvas.bbox("all")
            ),
        )

        self.names_canvas_window = self.names_canvas.create_window(
            (0, 0), window=self.names_inner, anchor="nw"
        )

        self.names_canvas.bind(
            "<Configure>",
            lambda e: self.names_canvas.itemconfigure(
                self.names_canvas_window, width=e.width
            ),
        )

        self.names_canvas.configure(yscrollcommand=self.names_scrollbar.set)
        self.names_canvas.pack(side="left", fill="both", expand=True)
        self.names_scrollbar.pack(side="right", fill="y")

        ttk.Label(
            outer, text="Output folder", font=(UI_FONT, 10, "bold")
        ).grid(row=10, column=0, sticky="w", pady=(16, 4))

        ttk.Entry(
            outer, textvariable=self.output
        ).grid(row=11, column=0, sticky="ew", padx=(0, 8))

        ttk.Button(
            outer, text="Browse...", command=self.choose_output
        ).grid(row=11, column=1)

        ttk.Button(
            outer,
            text="Start Analysis",
            command=self.start,
        ).grid(row=12, column=1, pady=(22, 0), ipadx=14, ipady=5)

        ttk.Separator(outer).grid(
            row=13, column=0, columnspan=2, sticky="ew", pady=(20, 10)
        )

        ttk.Label(
            outer,
            textvariable=self.status,
            wraplength=670,
        ).grid(row=14, column=0, columnspan=2, sticky="w")

        outer.rowconfigure(9, weight=1)

    def show_about(self):
        messagebox.showinfo(
            "About " + APP_NAME,
            (
                f"{APP_NAME}\n"
                f"{VERSION}\n\n"
                f"Developed by {AUTHOR}\n"
                f"{LAB}\n"
                f"{INSTITUTION}"
            ),
            parent=self.root,
        )

    def choose_image(self):
        path = filedialog.askopenfilename(
            title="Select original radiometric FLIR JPG",
            filetypes=[
                ("JPEG images", "*.jpg *.jpeg"),
                ("All files", "*.*"),
            ],
        )

        if not path:
            return

        candidate = Path(path)

        self.image_status.set(
            "Loading and checking radiometric FLIR temperature data..."
        )
        self.status.set(
            "Checking image for radiometric FLIR temperature data..."
        )
        self.root.update_idletasks()

        try:
            thermal = get_thermal(candidate)
        except Exception as error:
            # Do not keep an invalid image selected.
            self.validated_image_path = None
            self.validated_thermal = None

            messagebox.showerror(
                "Unsupported Image",
                str(error),
                parent=self.root,
            )

            self.image_status.set(
                "Could not load this file as a radiometric FLIR image."
            )
            self.status.set(
                "Image not selected. Please choose an original "
                "radiometric FLIR JPG."
            )
            return

        # Image passed radiometric validation.
        self.image.set(str(candidate))
        self.validated_image_path = candidate.resolve()
        self.validated_thermal = thermal

        if not self.output.get().strip():
            self.output.set(str(candidate.parent / "output"))

        self.image_status.set(
            f"Ready: {candidate.name}  "
            f"({thermal.shape[1]} x {thermal.shape[0]} thermal pixels)"
        )
        self.status.set(
            f"Valid radiometric FLIR image: {candidate.name}"
        )

    def choose_output(self):
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.output.set(path)

    def parse_count(self):
        text = self.count.get().strip()
        if not text:
            return None
        try:
            n = int(text)
            return n if n > 0 else None
        except ValueError:
            return None

    def on_setup_change(self, *args):
        if self.use_custom_names.get():
            self.rebuild_name_fields()

    def toggle_custom_names(self):
        if self.use_custom_names.get():
            self.names_container.grid()
            self.rebuild_name_fields()
        else:
            self.remember_current_names()
            self.names_container.grid_remove()

    def remember_current_names(self):
        values = [var.get() for var in self.name_vars]
        if len(values) > len(self.saved_name_values):
            self.saved_name_values.extend(
                [""] * (len(values) - len(self.saved_name_values))
            )
        for i, value in enumerate(values):
            self.saved_name_values[i] = value

    def rebuild_name_fields(self):
        self.remember_current_names()

        for widget in self.names_inner.winfo_children():
            widget.destroy()

        n = self.parse_count()
        self.name_vars = []

        if n is None:
            ttk.Label(
                self.names_inner,
                text="Enter the number of plants above to add custom labels.",
            ).pack(anchor="w", pady=6)
            return

        if len(self.saved_name_values) < n:
            self.saved_name_values.extend(
                [""] * (n - len(self.saved_name_values))
            )

        for i in range(1, n + 1):
            row = ttk.Frame(self.names_inner)
            row.pack(fill="x", pady=3)

            ttk.Label(
                row,
                text=f"Plant {i}",
                width=10,
            ).pack(side="left")

            var = tk.StringVar(value=self.saved_name_values[i - 1])

            # Keep saved values synchronized while user types.
            def save_value(*args, index=i - 1, variable=var):
                if len(self.saved_name_values) <= index:
                    self.saved_name_values.extend(
                        [""] * (index + 1 - len(self.saved_name_values))
                    )
                self.saved_name_values[index] = variable.get()

            var.trace_add("write", save_value)

            ttk.Entry(
                row,
                textvariable=var,
            ).pack(side="left", fill="x", expand=True)

            self.name_vars.append(var)

    def get_setup_names(self, n):
        if not self.use_custom_names.get():
            return [f"Plant {i}" for i in range(1, n + 1)]

        self.remember_current_names()
        names = []

        for i in range(n):
            raw = (
                self.saved_name_values[i].strip()
                if i < len(self.saved_name_values)
                else ""
            )
            names.append(raw if raw else f"Plant {i + 1}")

        return names

    def confirm_cancel_analysis(self):
        return confirm_in_window(
            "Cancel this analysis?",
            [
                "Selections made so far will not be saved.",
            ],
        )

    def review(self, image, mask, number, label, total, thermal):
        """Show the proposed mask and ask what to do with it.

        This used to be a small Tk dialog floating next to a separate OpenCV
        window. Two windows from two toolkits fought over focus, and on macOS
        the OpenCV one regularly came up behind or drew nothing at all
        because Tk owned the event loop. Everything now happens in the one
        analysis window, driven by the keyboard, like the outline step.
        """
        preview = image.copy()
        tint = np.zeros_like(preview)
        tint[:, :, 1] = 255

        idx = mask > 0
        preview[idx] = cv2.addWeighted(
            preview[idx], 0.6, tint[idx], 0.4, 0
        )

        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        cv2.drawContours(preview, contours, -1, (255, 255, 255), 2)

        # A running mean is a cheap sanity check: a mask that has slipped onto
        # the soil shows up immediately as a temperature well above the plant.
        try:
            summary = plant_stats(thermal, mask)
            right_text = (
                f"Mean {summary['mean']:.2f} C    {summary['pixels']} px"
            )
        except ValueError:
            right_text = "Empty mask"

        height, width = preview.shape[:2]
        display_w, display_h = display_size(width, width, height)

        view = cv2.resize(
            preview,
            (display_w, display_h),
            interpolation=cv2.INTER_CUBIC,
        )

        canvas = np.full(
            (HEADER_H + display_h, display_w, 3),
            242,
            dtype=np.uint8,
        )
        canvas[HEADER_H:, :] = view

        draw_header(
            canvas,
            (
                f"Review plant {number} of {total} - "
                f"{ascii_window_label(label)}"
            ),
            [
                "Does the green mask cover this plant?",
                "Enter / A: Accept    R: Redraw    "
                "E: Exclude soil or background    Esc: Cancel analysis",
            ],
            right_text=right_text,
        )

        ensure_workflow_window()
        try:
            cv2.setMouseCallback(WORKFLOW_WINDOW, lambda *args: None)
        except cv2.error:
            pass

        show_workflow(canvas)

        while True:
            if not workflow_window_open():
                return "CANCEL_REQUEST"

            key = cv2.waitKeyEx(20)

            if key == -1:
                continue

            # Enter / Return / A - accept and move on.
            if key in (13, 10, ord("a"), ord("A")):
                return True

            # Redraw this plant from scratch.
            if key in (ord("r"), ord("R")):
                return None

            # Cut soil or background out of the current mask.
            if key in (ord("e"), ord("E")):
                return False

            if key == 27:
                return "CANCEL_REQUEST"

    def start(self):
        # New analysis session: restore a comfortable default selection
        # window size instead of inheriting the previous session's size.
        self.reset_selection_window_next = True
        try:
            path_text = self.image.get().strip()
            count_text = self.count.get().strip()

            if not path_text:
                raise ValueError("Please select a radiometric FLIR JPG.")

            path = Path(path_text)
            if not path.exists():
                raise ValueError("Please select a valid FLIR JPG.")

            if not count_text:
                raise ValueError(
                    "Please enter the number of plants in this image."
                )

            try:
                n = int(count_text)
            except ValueError:
                raise ValueError(
                    "Number of plants must be a positive whole number."
                )

            if n < 1:
                raise ValueError(
                    "Number of plants must be a positive whole number."
                )

            plant_names = self.get_setup_names(n)

            # Every run gets its own timestamped folder inside the chosen
            # output location. Writing straight into that location meant a
            # second run on the same image silently overwrote the first one's
            # CSV and figures.
            out_root = Path(
                self.output.get().strip() or (path.parent / "output")
            )
            out = out_root / (
                f"{safe_filename(path.stem)}_"
                f"{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            )

            self.status.set("Reading radiometric FLIR data...")
            self.root.update()

            resolved_path = path.resolve()

            if (
                self.validated_image_path is not None
                and resolved_path == self.validated_image_path
                and self.validated_thermal is not None
            ):
                thermal = self.validated_thermal
            else:
                # This also protects users who manually typed/pasted a path
                # instead of choosing it with Browse.
                thermal = get_thermal(path)
                self.validated_image_path = resolved_path
                self.validated_thermal = thermal

            image = color_image(thermal)

            # Keep the setup window out of the way during selection/review.
            self.root.withdraw()

            masks = []
            results = []

            for i in range(1, n + 1):
                label = plant_names[i - 1]

                while True:
                    self.status.set(
                        f"Plant {i} of {n} - {label}: outline the plant."
                    )
                    self.root.update()

                    cv_label = ascii_window_label(label)
                    polygon = PolygonSelector(
                        image,
                        f"Plant {i} of {n} - {cv_label} - Outline Plant",
                        reset_window_size=self.reset_selection_window_next,
                    ).run()

                    # Only the first selection window of a new analysis
                    # session forces the default native window size.
                    self.reset_selection_window_next = False

                    if isinstance(polygon, str) and polygon == "CANCEL_REQUEST":
                        if self.confirm_cancel_analysis():
                            raise AnalysisCancelled()
                        continue

                    mask = polygon_mask(thermal.shape, polygon)

                    while True:
                        decision = self.review(
                            image, mask, i, label, n, thermal
                        )

                        if (
                            isinstance(decision, str)
                            and decision == "CANCEL_REQUEST"
                        ):
                            if self.confirm_cancel_analysis():
                                raise AnalysisCancelled()
                            continue

                        if decision is True:
                            break

                        if decision is None:
                            mask = None
                            break

                        exclusion = PolygonSelector(
                            image,
                            (
                                f"Plant {i} - {cv_label} - "
                                "Outline Soil/Background to Exclude"
                            ),
                        ).run()

                        if (
                            isinstance(exclusion, str)
                            and exclusion == "CANCEL_REQUEST"
                        ):
                            if self.confirm_cancel_analysis():
                                raise AnalysisCancelled()
                            continue

                        exclusion_mask = polygon_mask(
                            mask.shape, exclusion
                        )
                        mask[exclusion_mask > 0] = 0

                    if mask is not None and decision is True:
                        break

            # This line is intentionally outside the loop body only after
            # each plant has been accepted.
                masks.append(mask)
                results.append(plant_stats(thermal, mask))

            if len(masks) != n:
                raise RuntimeError(
                    f"{n} plants were requested, but only "
                    f"{len(masks)} were accepted."
                )

            self.session = {
                "path": path,
                "out": out,
                "thermal": thermal,
                "masks": masks,
                "results": results,
                "names": list(plant_names),
            }

            # Selection is finished; take the analysis window down before the
            # summary appears so the two are never on screen together.
            close_workflow_window()

            self.write_session_outputs()
            self.status.set(
                f"Complete: {n} plants analyzed. Saved to {out.name}."
            )

            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.show_completion_window()

        except AnalysisCancelled:
            close_workflow_window()

            self.status.set(
                "Analysis cancelled. Ready to change settings or start again."
            )
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            return

        except Exception as error:
            close_workflow_window()

            self.status.set("Analysis stopped.")
            self.root.deiconify()
            self.root.lift()

            messagebox.showerror(
                "Analysis Error",
                str(error),
                parent=self.root,
            )
            return

    def write_session_outputs(self):
        s = self.session
        path = s["path"]
        out = s["out"]
        masks = s["masks"]
        results = s["results"]
        names = s["names"]
        thermal = s["thermal"]

        out.mkdir(parents=True, exist_ok=True)

        # Remove all existing individual mask files for this image before
        # writing the current names. This ensures Edit Plant Names replaces
        # the previous naming state rather than leaving duplicate old files.
        for old_mask in out.glob(f"{path.stem}_Plant*_mask.png"):
            try:
                old_mask.unlink()
            except OSError:
                pass

        for i, (mask, label) in enumerate(zip(masks, names), start=1):
            cv2.imwrite(
                str(
                    out
                    / (
                        f"{path.stem}_Plant{i}_"
                        f"{safe_filename(label)}_mask.png"
                    )
                ),
                mask,
            )

        csv_path = out / f"{path.stem}_temperature_summary.csv"

        with open(csv_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "image",
                "plant_number",
                "plant_name",
                "mean_temperature_C",
                "trimmed_mean_2pct_each_tail_C",
                "median_temperature_C",
                "minimum_temperature_C",
                "maximum_temperature_C",
                "plant_pixel_count",
            ])

            for i, (label, result) in enumerate(
                zip(names, results), start=1
            ):
                writer.writerow([
                    path.name,
                    i,
                    label,
                    result["mean"],
                    result["trimmed"],
                    result["median"],
                    result["min"],
                    result["max"],
                    result["pixels"],
                ])

        self.save_analysis(
            path, thermal, masks, results, names, out
        )
        self.save_qc(
            path, thermal, masks, results, names, out
        )

    def show_completion_window(self):
        s = self.session

        dialog = tk.Toplevel(self.root)
        dialog.title("Analysis Complete")
        dialog.geometry("610x450")
        dialog.minsize(560, 390)
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="Analysis Complete",
            font=(UI_FONT, 16, "bold"),
        ).pack(anchor="w")

        ttk.Label(
            frame,
            text=f"{len(s['results'])} plants analyzed successfully.",
        ).pack(anchor="w", pady=(4, 12))

        table = ttk.Treeview(
            frame,
            columns=("num", "name", "mean", "trim"),
            show="headings",
            height=min(len(s["results"]), 9),
        )
        table.heading("num", text="#")
        table.heading("name", text="Plant name")
        table.heading("mean", text="Mean (C)")
        table.heading("trim", text="Trimmed mean (C)")

        table.column("num", width=45, anchor="center")
        table.column("name", width=230)
        table.column("mean", width=105, anchor="center")
        table.column("trim", width=125, anchor="center")

        for i, (label, result) in enumerate(
            zip(s["names"], s["results"]), start=1
        ):
            table.insert(
                "",
                "end",
                values=(
                    i,
                    label,
                    f"{result['mean']:.2f}",
                    f"{result['trimmed']:.2f}",
                ),
            )

        table.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text=f"Results saved to:\n{s['out']}",
            wraplength=550,
        ).pack(anchor="w", pady=(12, 12))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")

        ttk.Button(
            buttons,
            text="Edit Plant Names",
            command=lambda: self.edit_names_after_analysis(
                dialog, table
            ),
        ).pack(side="left")

        ttk.Button(
            buttons,
            text="Open Output Folder",
            command=lambda: self.open_output_folder(s["out"]),
        ).pack(side="left", padx=(8, 0))

        def close_completion():
            dialog.destroy()

        ttk.Button(
            buttons,
            text="Done",
            command=close_completion,
        ).pack(side="right")

        # Clicking the window X is exactly the same as Done.
        dialog.protocol("WM_DELETE_WINDOW", close_completion)
        fit_window(dialog, 610, 450)

    def edit_names_after_analysis(self, completion_dialog, table):
        s = self.session

        edit = tk.Toplevel(completion_dialog)
        edit.title("Edit Plant Names")
        edit.geometry("540x520")
        edit.minsize(500, 380)
        edit.transient(completion_dialog)
        edit.grab_set()

        # Use grid so the header and bottom buttons remain fixed while only
        # the middle list of plant names scrolls.
        edit.columnconfigure(0, weight=1)
        edit.rowconfigure(1, weight=1)

        header = ttk.Frame(edit, padding=(18, 18, 18, 8))
        header.grid(row=0, column=0, sticky="ew")

        ttk.Label(
            header,
            text="Edit Plant Names",
            font=(UI_FONT, 15, "bold"),
        ).pack(anchor="w")

        ttk.Label(
            header,
            text=(
                "Only labels will change. Temperature values, masks, and "
                "selected plant regions will not be recalculated."
            ),
            wraplength=490,
        ).pack(anchor="w", pady=(5, 0))

        middle = ttk.Frame(edit, padding=(18, 0, 18, 0))
        middle.grid(row=1, column=0, sticky="nsew")
        middle.columnconfigure(0, weight=1)
        middle.rowconfigure(0, weight=1)

        canvas = tk.Canvas(
            middle,
            highlightthickness=0,
        )
        scroll = ttk.Scrollbar(
            middle,
            orient="vertical",
            command=canvas.yview,
        )
        inner = ttk.Frame(canvas)

        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(
                scrollregion=canvas.bbox("all")
            ),
        )

        canvas_window = canvas.create_window(
            (0, 0),
            window=inner,
            anchor="nw",
        )

        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(
                canvas_window,
                width=e.width,
            ),
        )

        canvas.configure(yscrollcommand=scroll.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        vars_ = []

        for i, label in enumerate(s["names"], start=1):
            row = ttk.Frame(inner)
            row.pack(fill="x", pady=4)

            ttk.Label(
                row,
                text=f"Plant {i}",
                width=10,
            ).pack(side="left")

            var = tk.StringVar(value=label)

            ttk.Entry(
                row,
                textvariable=var,
            ).pack(
                side="left",
                fill="x",
                expand=True,
            )

            vars_.append(var)

        # Fixed footer: these buttons never scroll off-screen.
        footer = ttk.Frame(edit, padding=(18, 12, 18, 18))
        footer.grid(row=2, column=0, sticky="ew")

        def cancel_edit(event=None):
            edit.destroy()

        def save_changes(event=None):
            new_names = []

            for i, var in enumerate(vars_, start=1):
                value = var.get().strip()
                new_names.append(
                    value if value else f"Plant {i}"
                )

            # Update the in-memory names for this completed analysis.
            s["names"] = new_names

            # Also update the setup-page labels so the corrected names are
            # still visible if the user runs another analysis.
            self.use_custom_names.set(True)
            self.saved_name_values = list(new_names)
            self.names_container.grid()
            self.rebuild_name_fields()

            # Re-write ALL outputs from this analysis using the new labels.
            # Temperature values and masks are reused exactly as-is.
            self.write_session_outputs()

            # Refresh the Analysis Complete table.
            for item in table.get_children():
                table.delete(item)

            for i, (label, result) in enumerate(
                zip(s["names"], s["results"]),
                start=1,
            ):
                table.insert(
                    "",
                    "end",
                    values=(
                        i,
                        label,
                        f"{result['mean']:.2f}",
                        f"{result['trimmed']:.2f}",
                    ),
                )

            self.status.set(
                "Plant names updated. All outputs for this analysis "
                "were refreshed; temperature values were unchanged."
            )

            edit.destroy()

        ttk.Button(
            footer,
            text="Cancel",
            command=cancel_edit,
        ).pack(side="right")

        ttk.Button(
            footer,
            text="Save Changes",
            command=save_changes,
        ).pack(side="right", padx=(0, 8))

        # Keyboard shortcuts.
        edit.bind("<Return>", save_changes)
        edit.bind("<Escape>", cancel_edit)

        # Closing X behaves like Cancel here (no changes saved).
        edit.protocol("WM_DELETE_WINDOW", cancel_edit)
        fit_window(edit, 540, 520)

    def open_output_folder(self, path):
        path = str(Path(path))

        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def save_analysis(
        self,
        path,
        thermal,
        masks,
        results,
        names,
        out,
    ):
        # Matplotlib is intentionally imported only when output figures are
        # created so the main GUI can appear faster in packaged builds.
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(9, 7))
        image = ax.imshow(thermal, cmap="jet")
        fig.colorbar(image, ax=ax, label="Temperature (C)")

        for mask, result, label in zip(masks, results, names):
            contours, _ = cv2.findContours(
                mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )

            for contour in contours:
                points = contour[:, 0, :]
                ax.plot(
                    points[:, 0],
                    points[:, 1],
                    linewidth=1.5,
                )

            ys, xs = np.where(mask > 0)
            ax.text(
                xs.min(),
                max(10, ys.min() - 5),
                f"{label}: {result['mean']:.2f} C",
                bbox={
                    "boxstyle": "round,pad=0.25",
                    "alpha": 0.75,
                },
            )

        ax.set_title(f"{path.name} - Plant Thermal Analysis")
        ax.axis("off")
        fig.tight_layout()
        fig.savefig(
            out / f"{path.stem}_temperature_analysis.png",
            dpi=200,
            bbox_inches="tight",
        )
        plt.close(fig)

    def save_qc(
        self,
        path,
        thermal,
        masks,
        results,
        names,
        out,
    ):
        # Lazy import keeps Matplotlib out of the initial GUI startup path.
        import matplotlib.pyplot as plt

        base = cv2.cvtColor(
            color_image(thermal),
            cv2.COLOR_BGR2RGB,
        )

        base = np.clip(
            base.astype(float) * 0.70,
            0,
            255,
        ).astype(np.uint8)

        fig, ax = plt.subplots(figsize=(9, 7))
        ax.imshow(base)

        for mask, result, label in zip(masks, results, names):
            overlay = np.zeros((*mask.shape, 4), dtype=float)
            overlay[:, :, 1] = 1.0
            overlay[:, :, 3] = np.where(mask > 0, 0.20, 0.0)
            ax.imshow(overlay)

            contours, _ = cv2.findContours(
                mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )

            for contour in contours:
                points = contour[:, 0, :]
                ax.plot(
                    points[:, 0],
                    points[:, 1],
                    color="white",
                    linewidth=2,
                )

            ys, xs = np.where(mask > 0)
            ax.text(
                xs.min(),
                max(10, ys.min() - 5),
                (
                    f"{label}: {result['mean']:.2f} C "
                    f"(trim {result['trimmed']:.2f} C)"
                ),
                color="white",
                bbox={
                    "boxstyle": "round,pad=0.25",
                    "facecolor": "black",
                    "alpha": 0.72,
                },
            )

        ax.set_title("QC Mask Overlay")
        ax.axis("off")
        fig.tight_layout()
        fig.savefig(
            out / f"{path.stem}_qc_mask_overlay.png",
            dpi=200,
            bbox_inches="tight",
        )
        plt.close(fig)


def self_test(image_path=None):
    """Headless build check. Prints a report and returns a process exit code.

    Run on any target machine with:
        "<App>.app/Contents/MacOS/PlantThermalImageAnalyzer" --self-test [image]
    """
    ok = True

    print(f"{APP_NAME} {VERSION}")
    print(f"frozen        : {bool(getattr(sys, 'frozen', False))}")
    print(f"bundle dir    : {bundle_dir()}")
    print(f"exiftool path : {EXIFTOOL_PATH}")

    if exiftool_available():
        print("exiftool      : OK")
    else:
        print("exiftool      : FAILED")
        ok = False

    try:
        root = tk.Tk()
        root.withdraw()
        print(f"tk            : OK (Tk {root.tk.call('info', 'patchlevel')})")
        root.destroy()
    except Exception as exc:
        print(f"tk            : FAILED ({exc})")
        ok = False

    try:
        import time

        import matplotlib
        import matplotlib.font_manager as fm

        cache_dir = Path(matplotlib.get_cachedir())
        cache_file = cache_dir / f"fontlist-v{fm.FontManager.__version__}.json"
        print(f"mpl cachedir  : {cache_dir}")
        print(f"mpl fontcache : "
              f"{'present' if cache_file.is_file() else 'MISSING'}")

        start = time.perf_counter()
        import matplotlib.pyplot as plt

        figure = plt.figure()
        plt.close(figure)
        print(f"mpl figure    : OK ({time.perf_counter() - start:.1f}s)")
    except Exception as exc:
        print(f"matplotlib    : FAILED ({exc})")
        ok = False

    if image_path:
        try:
            data = get_thermal(Path(image_path))
            print(
                f"thermal decode: OK {data.shape} "
                f"min {float(data.min()):.2f} C max {float(data.max()):.2f} C"
            )
        except Exception as exc:
            print(f"thermal decode: FAILED ({exc})")
            ok = False
    else:
        print("thermal decode: skipped (no image given)")

    print("RESULT        :", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    clean_subprocess_env()

    if "--self-test" in sys.argv:
        index = sys.argv.index("--self-test")
        extra = sys.argv[index + 1:]
        return self_test(extra[0] if extra else None)

    root = tk.Tk()

    if IS_MAC:
        # A double-clicked .app otherwise opens behind whatever the user was
        # already looking at.
        root.lift()
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))

    App(root)

    if not exiftool_available():
        messagebox.showerror(
            "ExifTool Not Found",
            "This application needs ExifTool to read radiometric FLIR data, "
            "and it could not be started.\n\n"
            f"Tried: {EXIFTOOL_PATH}\n\n"
            "On macOS the bundled copy runs through the system Perl at "
            "/usr/bin/perl. If that is missing, install ExifTool with "
            "'brew install exiftool'.",
        )

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
