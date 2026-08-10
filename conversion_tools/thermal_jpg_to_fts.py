# -*- coding: utf-8 -*-
"""
FLIR JPG to FTS Converter

Converts a radiometric FLIR JPG image into an FTS file containing
the extracted thermal data.

Requirements:
    pip install flirextractor astropy

Notes:
    - ExifTool is required by flirextractor.
    - On Windows, install ExifTool and make sure it is available on PATH.
    - On macOS, you can install it with Homebrew:
          brew install exiftool

The .fts extension used here stores FITS-format data and is retained
for compatibility with the existing workflow.
"""

from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

from astropy.io import fits
from flirextractor import FlirExtractor


def convert_jpg_to_fts(input_path: str, output_path: str) -> None:
    """Convert one radiometric FLIR JPG image to an FTS thermal-data file."""
    with FlirExtractor() as extractor:
        thermal_data = extractor.get_thermal(input_path)

    hdu = fits.PrimaryHDU(thermal_data)
    hdu.writeto(output_path, overwrite=True)


def main() -> None:
    root = tk.Tk()
    root.withdraw()

    input_path = filedialog.askopenfilename(
        title="Select a FLIR thermal JPG image",
        filetypes=[
            ("JPEG images", "*.jpg *.jpeg"),
            ("All files", "*.*"),
        ],
    )

    if not input_path:
        return

    default_name = f"{Path(input_path).stem}.fts"

    output_path = filedialog.asksaveasfilename(
        title="Save FTS file as",
        defaultextension=".fts",
        initialfile=default_name,
        filetypes=[
            ("FTS files", "*.fts"),
            ("All files", "*.*"),
        ],
    )

    if not output_path:
        return

    try:
        convert_jpg_to_fts(input_path, output_path)
    except Exception as exc:
        messagebox.showerror(
            "Conversion failed",
            f"Could not convert the selected image.\n\n{exc}",
        )
        return

    messagebox.showinfo(
        "Conversion complete",
        f"FTS file saved successfully:\n\n{output_path}",
    )


if __name__ == "__main__":
    main()
