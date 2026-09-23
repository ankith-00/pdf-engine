"""
Step 4 — Generate Barcode: create a Code128 barcode PNG from a UUCMS string.

Uses python-barcode to generate a Code128 barcode, rendered to an in-memory
PNG via BytesIO. The barcode is sized to fit the detected QR cell rect with
appropriate margins.
"""

from __future__ import annotations

from io import BytesIO

import barcode
from PIL import Image, ImageChops
from barcode.writer import ImageWriter


def generate_barcode_png(
    uucms: str,
    cell_width: float,
    cell_height: float,
    margin_pt: float = 5.0,
    dpi: int = 300,
) -> bytes:
    """
    Generate a Code128 barcode PNG for the given UUCMS string.
    
    Args:
        uucms: The UUCMS registration number to encode (e.g. "P18DM23M015001").
        cell_width: Width of the target cell in PDF points.
        cell_height: Height of the target cell in PDF points.
        margin_pt: Margin in points on each side inside the cell.
        dpi: Resolution for the barcode image.
    
    Returns:
        PNG image bytes ready for insertion into the PDF.
    """
    # Create the Code128 barcode
    code128 = barcode.get_barcode_class("code128")

    # Configure the writer for proper sizing
    writer = ImageWriter()

    # Generate the barcode
    bc = code128(uucms, writer=writer)

    # Render to a BytesIO buffer
    buffer = BytesIO()
    bc.write(buffer, options={
        "module_width": 0.72,       # Wider bar modules for a broader barcode
        "module_height": 18.0,
        "quiet_zone": 2.0,          # Small quiet zone on sides
        "font_size": 10,            # Clear readable font size
        "text_distance": 5.0,       # Keep a visible gap between bars and text
        "dpi": dpi,
        "write_text": True,         # Show the UUCMS text below the barcode
    })

    # Remove only the outer white canvas. The white gap between bars and the
    # human-readable value remains part of the barcode image.
    with Image.open(buffer) as source_image:
        image = source_image.convert("RGB")
    content_bbox = ImageChops.difference(
        image,
        Image.new("RGB", image.size, "white"),
    ).getbbox()
    if content_bbox is not None:
        image = image.crop(content_bbox)

    output = BytesIO()
    image.save(output, format="PNG")
    image.close()
    buffer.close()
    return output.getvalue()
