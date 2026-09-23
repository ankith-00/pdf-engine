"""
Step 2 — Extract: pull student data from each hall ticket page.

Uses PyMuPDF's structured text output (dict mode) to find known label strings
and read their associated values by spatial proximity. Also parses the course
table and exam-centre table by detecting header rows and reading subsequent
row bands.

The extraction schema matches the design doc §2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

import fitz  # PyMuPDF


# ─── Data classes ────────────────────────────────────────────────────────────

@dataclass
class Course:
    sl_no: int = 0
    term: str = ""
    qp_code: str = ""
    course_code: str = ""
    course_name: str = ""
    exam_date: str = ""
    exam_time: str = ""
    centre_code: str = ""


@dataclass
class ExamCentre:
    sl_no: int = 0
    centre_code: str = ""
    centre_name: str = ""


@dataclass
class StudentRecord:
    academic_year: str = ""
    month_of_exam: str = ""
    college_name: str = ""
    college_code: str = ""
    program_level: str = ""
    program_name: str = ""
    discipline: str = ""
    semester: str = ""
    uucms: str = ""
    student_name: str = ""
    courses: list[Course] = field(default_factory=list)
    exam_centres: list[ExamCentre] = field(default_factory=list)
    source_page_range: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["timetable"] = data.pop("courses")
        return data


# Course table header keywords (in order) to detect the header row
_COURSE_HEADERS = ["Sl.No", "Term/Semester", "QP Code", "Course Code",
                   "Course Name", "Exam Date", "Centre Code"]

# Exam-centre table header keywords
_CENTRE_HEADERS = ["Sl.No", "Exam Centre Code", "Exam Centre Name"]

_COURSE_DATE_RE = re.compile(
    r"(?P<date>\d{2}\s*-\s*\d{2}\s*-\s*\d{4})\s*-?\s*"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap][Mm])?)"
)


# ─── Text span helpers ──────────────────────────────────────────────────────

def _get_text_lines(page: fitz.Page) -> list[dict]:
    """
    Get text organized as lines with bounding boxes.
    Returns list of {text, bbox, y_center} dicts.
    """
    lines = []
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            full_text = ""
            x0, y0, x1, y1 = float("inf"), float("inf"), 0, 0
            for span in line.get("spans", []):
                full_text += span.get("text", "")
                bbox = span.get("bbox", (0, 0, 0, 0))
                x0 = min(x0, bbox[0])
                y0 = min(y0, bbox[1])
                x1 = max(x1, bbox[2])
                y1 = max(y1, bbox[3])
            text = full_text.strip()
            if text:
                lines.append({
                    "text": text,
                    "bbox": (x0, y0, x1, y1),
                    "y_center": (y0 + y1) / 2,
                })
    return lines


# ─── Label/value extraction ─────────────────────────────────────────────────

def _extract_label_values(page: fitz.Page) -> dict[str, str]:
    """
    Extract key-value fields from the hall ticket header block.
    Parses line-by-line before the course table, handling multi-line college names.
    """
    text = page.get_text("text")
    lines = [re.sub(r"^[^a-zA-Z0-9]+", "", l.strip()) for l in text.splitlines() if l.strip()]

    result: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]

        # Stop once we reach the course table header
        if re.match(r"^Sl\.?\s*No\.?", line, re.IGNORECASE):
            break

        # Match "Label : Value"
        m = re.match(r"^([A-Za-z /]+)\s*:\s*(.*)$", line)
        if m:
            label = m.group(1).strip()
            val = m.group(2).strip()

            # If label is College Name and next line is continuation
            if "college name" in label.lower() and i + 1 < len(lines):
                next_l = lines[i + 1]
                if ":" not in next_l and not re.match(r"^Sl\.?\s*No", next_l, re.IGNORECASE):
                    val = f"{val} {next_l}".strip()
                    i += 1

            norm_label = label.lower()
            if "academic year" in norm_label:
                result["academic_year"] = val
            elif "month of exam" in norm_label:
                result["month_of_exam"] = val
            elif "college code" in norm_label:
                result["college_code"] = val
            elif "college name" in norm_label:
                result["college_name"] = val
            elif "program level" in norm_label:
                result["program_level"] = val
            elif "program name" in norm_label:
                result["program_name"] = val
            elif "discipline" in norm_label:
                result["discipline"] = val
            elif "term" in norm_label or "semester" in norm_label:
                result["semester"] = val
            elif "reg" in norm_label:
                result["uucms"] = val
            elif "student" in norm_label and "name" in norm_label:
                result["student_name"] = val

        i += 1

    # Fallback for UUCMS if not captured
    if not result.get("uucms"):
        uucms_match = re.search(r"\b([A-Z]\d{2}[A-Z]{2}\d{2}[A-Z]\d{6})\b", text)
        if uucms_match:
            result["uucms"] = uucms_match.group(1)

    return result


# ─── Table parsing ───────────────────────────────────────────────────────────

def _find_table_rows(
    lines: list[dict],
    header_keywords: list[str],
    stop_keywords: list[str] | None = None,
) -> tuple[int, list[list[dict]]]:
    """
    Find a table by locating its header row (matching header_keywords),
    then collecting subsequent lines as rows until a stop condition is met.
    
    Returns (header_line_index, list_of_row_groups) where each row_group is
    a list of lines that belong to the same table row (grouped by y-proximity).
    """
    if stop_keywords is None:
        stop_keywords = ["Signature", "Registrar", "Principal", "Exam Centre"]

    # Find the header line
    header_idx = -1
    header_y = 0
    for i, line in enumerate(lines):
        text = line["text"]
        # Check if this line contains enough header keywords
        matches = sum(1 for kw in header_keywords if kw.lower() in text.lower())
        if matches >= len(header_keywords) // 2 + 1:
            header_idx = i
            header_y = line["y_center"]
            break

    if header_idx < 0:
        return -1, []

    # Collect data lines below the header
    data_lines = []
    for i in range(header_idx + 1, len(lines)):
        line = lines[i]
        # Skip if line is above the header (shouldn't happen but safety check)
        if line["y_center"] <= header_y:
            continue
        # Stop at stop keywords
        if any(kw.lower() in line["text"].lower() for kw in stop_keywords):
            break
        data_lines.append(line)

    # Group lines into rows by y-proximity (lines within 5pt are same row)
    rows: list[list[dict]] = []
    current_row: list[dict] = []
    last_y = -999

    for line in sorted(data_lines, key=lambda l: l["y_center"]):
        if abs(line["y_center"] - last_y) > 8:  # new row
            if current_row:
                rows.append(current_row)
            current_row = [line]
        else:
            current_row.append(line)
        last_y = line["y_center"]
    if current_row:
        rows.append(current_row)

    return header_idx, rows


def _parse_course_rows(rows: list[list[dict]]) -> list[Course]:
    """Parse course table rows into Course objects."""
    courses = []

    logical_rows: list[str] = []
    current_row = ""
    for row_lines in rows:
        line_text = " ".join(
            line["text"] for line in sorted(row_lines, key=lambda item: item["bbox"][0])
        ).strip()
        if not line_text:
            continue

        if re.match(r"^\d+\s+", line_text):
            if current_row:
                logical_rows.append(current_row)
            current_row = line_text
        elif current_row:
            current_row = f"{current_row} {line_text}"
    if current_row:
        logical_rows.append(current_row)

    for full_text in logical_rows:
        prefix_match = re.match(r"^(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$", full_text)
        if not prefix_match:
            continue

        course = Course(
            sl_no=int(prefix_match.group(1)),
            term=prefix_match.group(2),
            qp_code=prefix_match.group(3),
            course_code=prefix_match.group(4),
        )
        details = prefix_match.group(5).strip()
        date_match = _COURSE_DATE_RE.search(details)

        if date_match:
            raw_date = date_match.group("date")
            day, month, year = raw_date.split("-")
            course.exam_date = f"{year}-{month}-{day}"
            course.exam_time = date_match.group("time").strip()
            course.course_name = details[:date_match.start()].strip(" -")
            course.centre_code = details[date_match.end():].strip(" -")
        else:
            course.course_name = details

        courses.append(course)
    return courses


def _parse_centre_rows(rows: list[list[dict]]) -> list[ExamCentre]:
    """Parse exam-centre table rows into ExamCentre objects."""
    centres = []
    for row_lines in rows:
        full_text = " ".join(l["text"] for l in sorted(row_lines, key=lambda x: x["bbox"][0]))
        full_text = full_text.strip()

        if not full_text or not any(c.isdigit() for c in full_text[:5]):
            continue

        parts = re.split(r"\s{2,}", full_text)
        centre = ExamCentre()

        if len(parts) >= 3:
            try:
                centre.sl_no = int(parts[0].strip())
            except ValueError:
                continue
            centre.centre_code = parts[1].strip()
            centre.centre_name = parts[2].strip()
        elif len(parts) == 2:
            try:
                centre.sl_no = int(parts[0].strip())
            except ValueError:
                continue
            # Code and name might be together
            centre.centre_code = parts[1].strip()

        centres.append(centre)
    return centres


def _clean_table_cell(value: object) -> str:
    """Normalize a cell returned by PyMuPDF's table extractor."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def _extract_native_tables(page: fitz.Page) -> tuple[list[Course], list[ExamCentre]]:
    """Extract timetable and exam-centre rows from detected PDF tables."""
    courses: list[Course] = []
    centres: list[ExamCentre] = []

    try:
        tables = page.find_tables().tables
    except Exception:
        return courses, centres

    for table in tables:
        rows = table.extract()
        if not rows:
            continue

        header = [_clean_table_cell(value).lower() for value in rows[0]]
        if "course name" in header and any("exam date" in value for value in header):
            for row in rows[1:]:
                cells = [_clean_table_cell(value) for value in row]
                if len(cells) < 7 or not cells[0].isdigit():
                    continue

                date_match = _COURSE_DATE_RE.search(cells[5])
                course = Course(
                    sl_no=int(cells[0]),
                    term=cells[1],
                    qp_code=cells[2],
                    course_code=cells[3],
                    course_name=cells[4],
                    centre_code=cells[6],
                )
                if date_match:
                    normalized_date = re.sub(r"\s+", "", date_match.group("date"))
                    day, month, year = normalized_date.split("-")
                    course.exam_date = f"{year}-{month}-{day}"
                    course.exam_time = date_match.group("time").strip()
                courses.append(course)

        elif "exam centre code" in header and "exam centre name" in header:
            for row in rows[1:]:
                cells = [_clean_table_cell(value) for value in row]
                if len(cells) < 3 or not cells[0].isdigit():
                    continue
                centres.append(ExamCentre(
                    sl_no=int(cells[0]),
                    centre_code=cells[1],
                    centre_name=cells[2],
                ))

    return courses, centres


# ─── Public API ──────────────────────────────────────────────────────────────

def extract_student(
    doc: fitz.Document,
    page_range: tuple[int, int],
) -> StudentRecord:
    """
    Extract a StudentRecord from the given page range of the document.
    
    Args:
        doc: The open PyMuPDF document.
        page_range: (start_page_index, end_page_index) inclusive.
    
    Returns:
        A populated StudentRecord.
    """
    start_page, end_page = page_range
    record = StudentRecord(source_page_range=[start_page + 1, end_page + 1])

    # Collect text from all pages in the range (usually just one)
    all_lines: list[dict] = []
    native_courses: list[Course] = []
    native_centres: list[ExamCentre] = []
    for page_idx in range(start_page, end_page + 1):
        page = doc[page_idx]

        # Extract label/value pairs from this page
        label_values = _extract_label_values(page)
        for field_name, value in label_values.items():
            if value:  # Don't overwrite with empty
                setattr(record, field_name, value)

        # Collect lines for table parsing
        page_lines = _get_text_lines(page)
        all_lines.extend(page_lines)
        page_courses, page_centres = _extract_native_tables(page)
        native_courses.extend(page_courses)
        native_centres.extend(page_centres)

    # Parse course table
    _, course_rows = _find_table_rows(
        all_lines,
        _COURSE_HEADERS,
        stop_keywords=["Signature", "Registrar", "Principal",
                        "Exam Centre", "Controller", "Invigilator"],
    )
    record.courses = native_courses or _parse_course_rows(course_rows)

    # Parse exam-centre table
    _, centre_rows = _find_table_rows(
        all_lines,
        _CENTRE_HEADERS,
        stop_keywords=["Signature", "Registrar", "Principal",
                        "Controller", "Note", "Invigilator"],
    )
    record.exam_centres = native_centres or _parse_centre_rows(centre_rows)

    return record
