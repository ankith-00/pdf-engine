"""
Step 6 — Merge: finalize the processed PDF into an in-memory buffer.

Since we modify pages in-place on the same fitz.Document object (no actual
split into separate PDFs), the "merge" step is really just saving the modified
document to a BytesIO buffer. This module exists as a clean abstraction point
in case future requirements need actual multi-document merging.
"""

from __future__ import annotations

import logging
from io import BytesIO

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

# MuPDF reports format problems in the SOURCE file straight to stderr as
# "MuPDF error: ..." while it rewrites the document on save. They are not
# Python exceptions and nothing in the pipeline can catch them. Route them
# into PyMuPDF's warnings buffer instead, and summarise them below.
fitz.TOOLS.mupdf_display_errors(False)

# The hall-ticket generator writes cross-reference entries for objects it
# never emits. MuPDF drops those entries when it rewrites the file; the output
# is valid. Each dropped entry produces one line matching this text.
_DANGLING_XREF = "cannot find object in xref"


def finalize_pdf(doc: fitz.Document) -> bytes:
    """
    Save the modified document to an in-memory buffer and return the bytes.
    
    Args:
        doc: The PyMuPDF document with barcodes already stamped.
    
    Returns:
        The final PDF as bytes, ready for streaming to the client.
    """
    buffer = BytesIO()
    logger.info("Finalizing PDF: starting MuPDF save")

    # Clear anything earlier steps left in the buffer so the summary below
    # only describes this save.
    fitz.TOOLS.mupdf_warnings(reset=True)

    # Avoid garbage collection: malformed source PDFs make it disproportionately
    # expensive, while the rewritten output remains valid without it.
    doc.save(
        buffer,
        garbage=0,
        deflate=True,
        clean=False,
    )
    logger.info("Finalizing PDF: MuPDF save completed")

    messages = fitz.TOOLS.mupdf_warnings(reset=True)
    if messages:
        lines = messages.splitlines()
        dangling = sum(1 for line in lines if _DANGLING_XREF in line)
        other = [line for line in lines if _DANGLING_XREF not in line]
        if dangling:
            logger.warning(
                "Source PDF had %d dangling xref entries; MuPDF dropped them "
                "on save (output is valid).",
                dangling,
            )
        if other:
            # Anything that is not the known-harmless defect deserves to be
            # seen in full, but capped so a badly broken file cannot flood
            # the log.
            logger.warning(
                "MuPDF reported %d other message(s) while saving: %s",
                len(other),
                " | ".join(other[:5]),
            )

    return buffer.getvalue()
