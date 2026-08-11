# -*- coding: utf-8 -*-
"""
FLIR JPG to CSV Converter

Converts a radiometric FLIR JPG image into a CSV matrix containing
temperature values.

Requirements:
    pip install flirimageextractor numpy

Notes:
    - ExifTool is required by flirimageextractor.
    - On Windows, install ExifTool and make sure it is available on PATH.
    - On macOS, you can install it with Homebrew:
          brew install exiftool
"""

from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import numpy as np
from flirimageextractor import FlirImageExtractor


def extract_thermal_data(input_path: str):
    """Extract the thermal temperature matrix from one radiometric FLIR JPG."""
    flir = FlirImageExtractor()
    flir.process_image(flir_img_file=input_path)
    return flir.get_thermal_np()


def convert_jpg_to_csv(input_path: str, output_path: str) -> None:
    """Convert one radiometric FLIR JPG image to a CSV temperature matrix."""
    thermal_data = extract_thermal_data(input_path)
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
