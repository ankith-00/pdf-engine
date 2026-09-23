"""
Step 1 — Split: identify per-student page ranges.

The UUCMS hall ticket PDF has one ticket per page (confirmed across 320 pages).
This module returns a list of (start_page, end_page) tuples — currently always
(i, i) since each page is one student. The abstraction exists so that if future
files have multi-page tickets, we can detect the "Bengaluru City University"
title + underline rect as a page-break marker without changing downstream code.
"""

from __future__ import annotations

import fitz  # PyMuPDF


# The title text that marks the start of every hall ticket
_TITLE_MARKER = "Bengaluru City University"


def split_by_student(doc: fitz.Document) -> list[tuple[int, int]]:
    """
    Return a list of (start_page_index, end_page_index) tuples, one per student.

    Current implementation: one page = one student (trivial split).
    Future-proof: if a ticket spans multiple pages, consecutive pages without
    the title marker are grouped with the preceding title page.
    """
    page_ranges: list[tuple[int, int]] = []
    current_start: int | None = None

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        text = page.get_text("text")

        # Check if this page starts a new ticket
        if _TITLE_MARKER in text:
            # Close the previous range if one was open
            if current_start is not None:
                page_ranges.append((current_start, page_idx - 1))
            current_start = page_idx

    # Close the last range
    if current_start is not None:
        page_ranges.append((current_start, len(doc) - 1))

    # Fallback: if no title markers found at all, treat each page as one student
    if not page_ranges:
        page_ranges = [(i, i) for i in range(len(doc))]

    return page_ranges
