# -*- coding: utf-8 -*-
"""
FLIR JPG to CSV Converter

Converts a radiometric FLIR JPG image into a CSV matrix containing
temperature values.

Requirements:
    pip install flirextractor numpy

Notes:
    - ExifTool is required by flirextractor.
    - On Windows, install ExifTool and make sure it is available on PATH.
    - On macOS, you can install it with Homebrew:
          brew install exiftool
"""

from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import numpy as np
from flirextractor import FlirExtractor


def convert_jpg_to_csv(input_path: str, output_path: str) -> None:
    """Convert one radiometric FLIR JPG image to a CSV temperature matrix."""
    with FlirExtractor() as extractor:
        thermal_data = extractor.get_thermal(input_path)

    np.savetxt(output_path, thermal_data, delimiter=",")


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

    default_name = f"{Path(input_path).stem}.csv"

    output_path = filedialog.asksaveasfilename(
        title="Save CSV file as",
        defaultextension=".csv",
        initialfile=default_name,
        filetypes=[
            ("CSV files", "*.csv"),
            ("All files", "*.*"),
        ],
    )

    if not output_path:
        return

    try:
        convert_jpg_to_csv(input_path, output_path)
    except Exception as exc:
        messagebox.showerror(
            "Conversion failed",
            f"Could not convert the selected image.\n\n{exc}",
        )
        return

    messagebox.showinfo(
        "Conversion complete",
        f"CSV file saved successfully:\n\n{output_path}",
    )


if __name__ == "__main__":
    main()
