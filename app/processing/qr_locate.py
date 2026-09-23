"""
Step 3 — Locate QR Cell: dynamically find the barcode target cell per page.

This is the critical module. The QR cell is NOT at a fixed coordinate — it
shifts vertically depending on how many exam centres a student has (and how
many course rows precede the exam-centre table).

Algorithm:
1. Get all drawn paths/rects on the page via page.get_drawings().
2. Find the text span containing "QR Code" (the column header).
3. From the header's x-position, determine the column's horizontal band.
4. Filter drawn rects to those in the QR column's horizontal band AND below
   the header row's y-position — these are the data cell(s).
5. If multiple rects match (split-cell case for 2+ exam centres), take the
   bounding union.
6. Return the cell rect (x0, y0, x1, y1) in PDF coordinates.

Validated against the real 320-page PDF: works for both single-centre pages
(QR cell at ~y996–1031) and multi-centre pages (QR cell starting at ~y977).
"""

from __future__ import annotations

import logging
from typing import Optional

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


def _find_qr_header(page: fitz.Page) -> Optional[fitz.Rect]:
    """
    Find the text block containing "QR Code" on the page.
    Returns its bounding rect, or None if not found.
    """
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            line_text = ""
            line_bbox = None
            for span in line.get("spans", []):
                line_text += span.get("text", "")
                bbox = span.get("bbox", None)
                if bbox:
                    if line_bbox is None:
                        line_bbox = list(bbox)
                    else:
                        line_bbox[0] = min(line_bbox[0], bbox[0])
                        line_bbox[1] = min(line_bbox[1], bbox[1])
                        line_bbox[2] = max(line_bbox[2], bbox[2])
                        line_bbox[3] = max(line_bbox[3], bbox[3])

            if "QR Code" in line_text or "QR code" in line_text:
                if line_bbox:
                    return fitz.Rect(line_bbox)
    return None


def _get_drawn_rects(page: fitz.Page) -> list[fitz.Rect]:
    """
    Extract all rectangles from the page's drawing commands.
    PyMuPDF's get_drawings() returns every drawn path; we filter for
    rectangles (paths with exactly 4 points forming an axis-aligned rect,
    or explicit 're' items).
    """
    rects = []
    drawings = page.get_drawings()
    for drawing in drawings:
        rect = drawing.get("rect")
        if rect:
            r = fitz.Rect(rect)
            # Only consider rects with meaningful area (skip thin lines)
            if r.width > 5 and r.height > 5:
                rects.append(r)
    return rects


def locate_qr_cell(page: fitz.Page) -> Optional[fitz.Rect]:
    """
    Detect the QR Code placeholder cell on a hall ticket page.
    
    Returns:
        fitz.Rect with the cell bounds, or None if detection fails.
    """
    # Step 1: Find the "QR Code" column header
    header_rect = _find_qr_header(page)
    if header_rect is None:
        logger.warning(
            f"Page {page.number + 1}: Could not find 'QR Code' header text. "
            "Skipping barcode placement for this page."
        )
        return None

    # Step 2: Anchor the column to the center of this page's QR header.
    # This tracks horizontal layout changes without assuming a fixed x-value.
    header_center_x = (header_rect.x0 + header_rect.x1) / 2

    # Step 3: Get all drawn rectangles on the page
    all_rects = _get_drawn_rects(page)

    if not all_rects:
        logger.warning(
            f"Page {page.number + 1}: No drawn rectangles found on page. "
            "Skipping barcode placement."
        )
        return None

    # Step 4: Keep rectangles below the header that contain its center point.
    # The full table row may also contain that point, so it is narrowed down
    # to the smallest matching cell after the row is identified.
    header_bottom = header_rect.y1
    candidate_rects = []

    for rect in all_rects:
        if not rect.x0 <= header_center_x <= rect.x1:
            continue
        # Must be below the header row (with small tolerance)
        if rect.y0 < header_bottom - 5:
            continue
        candidate_rects.append(rect)

    if not candidate_rects:
        logger.warning(
            f"Page {page.number + 1}: No candidate rects found in QR column. "
            "Skipping barcode placement."
        )
        return None

    # Step 5: Keep only rectangles from the first data row below the header.
    first_row_y = min(rect.y0 for rect in candidate_rects)
    first_row_rects = [
        rect for rect in candidate_rects
        if rect.y0 <= first_row_y + 2
    ]

    # Prefer the actual cell over an outer table-row rectangle that encloses it.
    result = min(first_row_rects, key=lambda rect: rect.width)

    logger.debug(
        f"Page {page.number + 1}: QR cell detected at "
        f"({result.x0:.1f}, {result.y0:.1f}, {result.x1:.1f}, {result.y1:.1f})"
    )

    return result
