from __future__ import annotations

import struct
import zlib
from functools import lru_cache
from math import ceil
from pathlib import Path


PAGE_WIDTH = 612
PAGE_HEIGHT = 792
LEFT_MARGIN = 54
TOP_MARGIN = 72
BOTTOM_MARGIN = 54
LINE_HEIGHT = 14
TITLE_SIZE = 16
BODY_SIZE = 11

CARD_WIDTH = 760
CARD_HEIGHT = 430
CARD_MARGIN = 24

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHOOL_LOGO_PATH = REPO_ROOT / "assets" / "images" / "sch-logo.png"


def _rgb(red: float, green: float, blue: float) -> str:
    return f"{red:.3f} {green:.3f} {blue:.3f}"


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _estimate_text_width(text: str, size: float, *, bold: bool = False) -> float:
    if not text:
        return 0
    factor = 0.56 if bold else 0.52
    return len(text) * size * factor


def _fit_font_size(text: str, max_width: float, start_size: float, *, bold: bool = False, minimum: float = 6.5) -> float:
    estimated_width = _estimate_text_width(text, start_size, bold=bold)
    if estimated_width <= max_width:
        return start_size
    scaled = start_size * max_width / estimated_width
    return max(minimum, scaled)


def _wrap_pdf_text(text: str, max_width: float, size: float, *, bold: bool = False, max_lines: int | None = None) -> list[str]:
    value = (text or "").strip()
    if not value:
        return [""]

    words = value.split()
    lines: list[str] = []
    current = words[0]

    for word in words[1:]:
        candidate = f"{current} {word}"
        if _estimate_text_width(candidate, size, bold=bold) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word

    lines.append(current)

    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1].rstrip()
        if not last.endswith("..."):
            last = f"{last}..."
        lines[-1] = last

    return lines


def _text_lines(x: float, y: float, text: str, *, font: str = "F1", size: float = 10, color: str | None = None):
    commands = []
    if color:
        commands.append(color)
    commands.extend(
        [
            "BT",
            f"/{font} {size} Tf",
            f"{x:.2f} {y:.2f} Td",
            f"({_escape_pdf_text(text)}) Tj",
            "ET",
        ]
    )
    return commands


def _centered_text(center_x: float, y: float, text: str, *, font: str = "F1", size: float = 10, color: str | None = None, bold: bool = False):
    width = _estimate_text_width(text, size, bold=bold)
    return _text_lines(max(0, center_x - (width / 2)), y, text, font=font, size=size, color=color)


def _rect_commands(
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    fill: str | None = None,
    stroke: str | None = None,
    line_width: float | None = None,
):
    commands = []
    if line_width is not None:
        commands.append(f"{line_width} w")
    if fill:
        commands.append(f"{fill} rg")
    if stroke:
        commands.append(f"{stroke} RG")
    commands.append(f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re")
    if fill and stroke:
        commands.append("B")
    elif fill:
        commands.append("f")
    else:
        commands.append("S")
    return commands


def _line_commands(x1: float, y1: float, x2: float, y2: float, *, stroke: str, line_width: float = 1):
    return [
        f"{line_width} w",
        f"{stroke} RG",
        f"{x1:.2f} {y1:.2f} m",
        f"{x2:.2f} {y2:.2f} l",
        "S",
    ]


@lru_cache(maxsize=1)
def _load_school_logo():
    if not SCHOOL_LOGO_PATH.exists():
        return None

    data = SCHOOL_LOGO_PATH.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None

    position = 8
    width = height = bit_depth = color_type = None
    idat_parts: list[bytes] = []

    while position + 8 <= len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        position += 4
        chunk_type = data[position : position + 4]
        position += 4
        chunk_data = data[position : position + length]
        position += length + 4

        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", chunk_data)
            if compression != 0 or filter_method != 0 or interlace != 0:
                return None
        elif chunk_type == b"IDAT":
            idat_parts.append(chunk_data)
        elif chunk_type == b"IEND":
            break

    if not width or not height or bit_depth != 8 or color_type not in {2, 6}:
        return None

    raw = zlib.decompress(b"".join(idat_parts))
    channels = 3 if color_type == 2 else 4
    stride = width * channels
    pixels = bytearray()
    previous_row = bytearray(stride)
    index = 0

    for _ in range(height):
        if index >= len(raw):
            return None
        filter_type = raw[index]
        index += 1
        row = bytearray(raw[index : index + stride])
        index += stride

        if filter_type == 1:
            for i in range(channels, stride):
                row[i] = (row[i] + row[i - channels]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                row[i] = (row[i] + previous_row[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                up = previous_row[i]
                row[i] = (row[i] + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                up = previous_row[i]
                up_left = previous_row[i - channels] if i >= channels else 0
                predictor = left + up - up_left
                pa = abs(predictor - left)
                pb = abs(predictor - up)
                pc = abs(predictor - up_left)
                nearest = left if pa <= pb and pa <= pc else up if pb <= pc else up_left
                row[i] = (row[i] + nearest) & 0xFF
        elif filter_type != 0:
            return None

        if channels == 4:
            for i in range(0, stride, 4):
                red, green, blue, alpha = row[i : i + 4]
                pixels.extend(
                    [
                        (red * alpha + 255 * (255 - alpha)) // 255,
                        (green * alpha + 255 * (255 - alpha)) // 255,
                        (blue * alpha + 255 * (255 - alpha)) // 255,
                    ]
                )
        else:
            pixels.extend(row)

        previous_row = row

    return width, height, zlib.compress(bytes(pixels))


def _build_image_object(image_data: bytes, width: int, height: int) -> bytes:
    return (
        f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
        "/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
        f"/Length {len(image_data)} >>\nstream\n".encode("ascii")
        + image_data
        + b"\nendstream"
    )


def build_exam_card_pdf(
    *,
    school_name: str,
    card_title: str,
    subtitle: str | None,
    session_label: str | None = None,
    fields: list[tuple[str, str]],
    reference: str,
    registered_courses: list[str] | None = None,
    footer: str | None = None,
    portrait_text: str | None = None,
) -> bytes:
    student_rows = fields[:8] or [("Details", "")]
    course_entries = [course.strip() for course in (registered_courses or []) if course and course.strip()]
    if not course_entries:
        course_entries = ["No registered courses found."]
    wrapped_courses = [_wrap_pdf_text(course, 660, 9.0, max_lines=2) for course in course_entries]
    course_line_count = sum(len(lines) for lines in wrapped_courses)
    course_box_height = max(96, 24 + (course_line_count * 14))
    session_height = 34 if session_label else 0
    section_gap = 12
    card_height = 456 + course_box_height + session_height + (section_gap if session_label else 0)

    logo = _load_school_logo()
    bottom_margin = 24
    footer_height = 46
    student_height = 176
    course_y = bottom_margin + footer_height + section_gap
    if session_label:
        session_y = course_y + course_box_height + section_gap
        student_y = session_y + session_height + section_gap
    else:
        session_y = None
        student_y = course_y + course_box_height + section_gap

    commands: list[str] = ["q"]
    commands.extend(_rect_commands(0, 0, CARD_WIDTH, card_height, fill=_rgb(0.995, 0.997, 0.992), stroke=_rgb(0.13, 0.52, 0.22), line_width=2.2))
    commands.extend(_line_commands(CARD_MARGIN, card_height - 40, CARD_WIDTH - CARD_MARGIN, card_height - 40, stroke=_rgb(0.13, 0.52, 0.22), line_width=0.8))

    if logo:
        logo_width, logo_height, _ = logo
        render_width = 58
        render_height = render_width * (logo_height / logo_width)
        logo_x = (CARD_WIDTH - render_width) / 2
        logo_y = card_height - 74
        commands.extend(
            [
                "q",
                f"{render_width:.2f} 0 0 {render_height:.2f} {logo_x:.2f} {logo_y:.2f} cm",
                "/Im1 Do",
                "Q",
            ]
        )
    else:
        commands.extend(
            [
                "0.13 0.52 0.22 rg",
                f"{(CARD_WIDTH / 2) - 18:.2f} {card_height - 74:.2f} 36 24 re f",
            ]
        )
        commands.extend(_centered_text(CARD_WIDTH / 2, card_height - 66, "EDU", font="F2", size=11, color=_rgb(1, 1, 1), bold=True))

    school_y = card_height - 98
    title_y = card_height - 116
    subtitle_y = card_height - 134

    commands.extend(_centered_text(CARD_WIDTH / 2, school_y, school_name, font="F2", size=12.3, color=_rgb(0.12, 0.12, 0.12), bold=True))
    commands.extend(_line_commands(200, school_y - 4, CARD_WIDTH - 200, school_y - 4, stroke=_rgb(0.45, 0.45, 0.45), line_width=0.4))
    commands.extend(_centered_text(CARD_WIDTH / 2, title_y, card_title, font="F2", size=10.4, color=_rgb(0.09, 0.16, 0.29), bold=True))
    commands.extend(_line_commands(270, title_y - 3, CARD_WIDTH - 270, title_y - 3, stroke=_rgb(0.69, 0.69, 0.69), line_width=0.35))
    if subtitle:
        commands.extend(_centered_text(CARD_WIDTH / 2, subtitle_y, subtitle, font="F1", size=8.4, color=_rgb(0.22, 0.22, 0.22)))

    badge_x = CARD_WIDTH - 162
    badge_y = card_height - 112
    commands.extend(_rect_commands(badge_x, badge_y, 128, 64, fill=_rgb(0.95, 0.99, 0.95), stroke=_rgb(0.13, 0.52, 0.22), line_width=1.0))
    commands.extend(_text_lines(badge_x + 12, badge_y + 45, "STATUS", font="F2", size=7.6, color=_rgb(0.13, 0.52, 0.22)))
    commands.extend(_text_lines(badge_x + 12, badge_y + 28, "PAID", font="F2", size=16, color=_rgb(0.12, 0.12, 0.12)))
    reference_size = _fit_font_size(reference, 104, 7.1)
    commands.extend(_text_lines(badge_x + 12, badge_y + 12, f"REF {reference}", font="F1", size=reference_size, color=_rgb(0.22, 0.22, 0.22)))

    photo_x = CARD_MARGIN + 2
    photo_y = student_y + 12
    photo_w = 122
    photo_h = student_height - 24
    commands.extend(_rect_commands(photo_x, photo_y, photo_w, photo_h, fill=_rgb(0.985, 0.995, 0.985), stroke=_rgb(0.38, 0.38, 0.38), line_width=1.0))
    commands.extend(_rect_commands(photo_x + 4, photo_y + 4, photo_w - 8, photo_h - 8, fill=_rgb(1, 1, 1), stroke=_rgb(0.72, 0.72, 0.72), line_width=0.6))
    portrait_label = portrait_text or "STUDENT"
    initials = "".join(part[0] for part in portrait_label.split()[:2]).upper() or "ST"
    commands.extend(_centered_text(photo_x + (photo_w / 2), photo_y + 88, initials, font="F2", size=26, color=_rgb(0.13, 0.52, 0.22), bold=True))
    commands.extend(_centered_text(photo_x + (photo_w / 2), photo_y + 68, portrait_label, font="F1", size=8.2, color=_rgb(0.25, 0.25, 0.25)))
    commands.extend(_centered_text(photo_x + (photo_w / 2), photo_y + 18, "STUDENT PHOTO", font="F2", size=7.1, color=_rgb(0.3, 0.3, 0.3), bold=True))

    table_x = 172
    table_y = student_y + 12
    table_w = CARD_WIDTH - table_x - CARD_MARGIN - 2
    table_h = student_height - 24
    row_h = table_h / len(student_rows)
    label_w = 150
    value_w = table_w - label_w

    for index, (label, value) in enumerate(student_rows):
        row_y = table_y + table_h - ((index + 1) * row_h)
        row_fill = _rgb(0.995, 0.998, 0.994) if index % 2 else _rgb(1, 1, 1)
        commands.extend(_rect_commands(table_x, row_y, table_w, row_h, fill=row_fill, stroke=_rgb(0.58, 0.58, 0.58), line_width=0.8))
        commands.extend(_rect_commands(table_x, row_y, label_w, row_h, fill=_rgb(0.92, 0.97, 0.93), stroke=_rgb(0.58, 0.58, 0.58), line_width=0.8))
        commands.extend(_line_commands(table_x + label_w, row_y, table_x + label_w, row_y + row_h, stroke=_rgb(0.58, 0.58, 0.58), line_width=0.6))

        label_size = _fit_font_size(label, label_w - 12, 9.0, bold=True)
        value_size = _fit_font_size(value, value_w - 14, 9.4)
        baseline = row_y + (row_h / 2) - 3.4
        commands.extend(_text_lines(table_x + 10, baseline, label, font="F2", size=label_size, color=_rgb(0.1, 0.1, 0.1)))
        commands.extend(_text_lines(table_x + label_w + 10, baseline, value or "-", font="F1", size=value_size, color=_rgb(0.18, 0.18, 0.18)))

    if session_label and session_y is not None:
        commands.extend(_rect_commands(CARD_MARGIN, session_y, CARD_WIDTH - (CARD_MARGIN * 2), session_height, fill=_rgb(0.95, 0.99, 0.95), stroke=_rgb(0.13, 0.52, 0.22), line_width=0.9))
        commands.extend(_text_lines(CARD_MARGIN + 14, session_y + 12, "SESSION", font="F2", size=8.0, color=_rgb(0.13, 0.52, 0.22)))
        session_text_size = _fit_font_size(session_label, CARD_WIDTH - (CARD_MARGIN * 2) - 116, 10.0, bold=True)
        commands.extend(_text_lines(CARD_MARGIN + 90, session_y + 11, session_label, font="F2", size=session_text_size, color=_rgb(0.12, 0.12, 0.12)))

    commands.extend(_rect_commands(CARD_MARGIN, course_y, CARD_WIDTH - (CARD_MARGIN * 2), course_box_height, fill=_rgb(0.98, 0.99, 0.98), stroke=_rgb(0.58, 0.58, 0.58), line_width=0.9))
    commands.extend(_rect_commands(CARD_MARGIN, course_y + course_box_height - 24, CARD_WIDTH - (CARD_MARGIN * 2), 24, fill=_rgb(0.13, 0.52, 0.22), stroke=_rgb(0.13, 0.52, 0.22), line_width=0.9))
    commands.extend(_text_lines(CARD_MARGIN + 14, course_y + course_box_height - 8, "REGISTERED COURSES", font="F2", size=8.4, color=_rgb(1, 1, 1)))

    course_text_y = course_y + course_box_height - 40
    for index, lines in enumerate(wrapped_courses, start=1):
        for line_index, line in enumerate(lines):
            prefix = f"{index}. " if line_index == 0 else "   "
            line_size = 9.0 if line_index == 0 else 8.6
            commands.extend(
                _text_lines(
                    CARD_MARGIN + 16,
                    course_text_y,
                    f"{prefix}{line}",
                    font="F1",
                    size=line_size,
                    color=_rgb(0.18, 0.18, 0.18),
                )
            )
            course_text_y -= 14

    commands.extend(_rect_commands(CARD_MARGIN, bottom_margin, CARD_WIDTH - (CARD_MARGIN * 2), footer_height, fill=_rgb(0.98, 0.99, 0.98), stroke=_rgb(0.72, 0.72, 0.72), line_width=0.8))
    commands.extend(_text_lines(CARD_MARGIN + 14, bottom_margin + 28, "IMPORTANT", font="F2", size=8.2, color=_rgb(0.13, 0.52, 0.22)))
    footer_text = footer or "Present this card with a valid student ID at the examination venue."
    footer_size = _fit_font_size(footer_text, 420, 8.9)
    commands.extend(_text_lines(CARD_MARGIN + 14, bottom_margin + 14, footer_text, font="F1", size=footer_size, color=_rgb(0.18, 0.18, 0.18)))
    commands.extend(_line_commands(CARD_WIDTH - 226, bottom_margin + 18, CARD_WIDTH - 50, bottom_margin + 18, stroke=_rgb(0.35, 0.35, 0.35), line_width=0.6))
    commands.extend(_text_lines(CARD_WIDTH - 220, bottom_margin + 28, "Signature", font="F2", size=8.0, color=_rgb(0.18, 0.18, 0.18)))
    commands.extend(_text_lines(CARD_WIDTH - 220, bottom_margin + 10, "........................................", font="F1", size=8.2, color=_rgb(0.18, 0.18, 0.18)))
    commands.append("Q")

    content = "\n".join(commands).encode("latin-1", errors="replace")
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    image_object_id = None
    if logo:
        logo_width, logo_height, logo_bytes = logo
        image_object_id = len(objects) + 1
        objects.append(_build_image_object(logo_bytes, logo_width, logo_height))

    content_object_id = len(objects) + 1
    objects.append(b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream")

    page_object_id = len(objects) + 1
    resources = ["/Font << /F1 3 0 R /F2 4 0 R >>"]
    if image_object_id:
        resources.append(f"/XObject << /Im1 {image_object_id} 0 R >>")
    objects.append(
        (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] "
            "/Resources << {resources} >> /Contents {content} 0 R >>"
        ).format(
            w=CARD_WIDTH,
            h=card_height,
            resources=" ".join(resources),
            content=content_object_id,
        ).encode("ascii")
    )
    objects[1] = f"<< /Type /Pages /Kids [{page_object_id} 0 R] /Count 1 >>".encode("ascii")

    pdf = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets = [0]
    current_offset = len(pdf[0])
    for object_number, body in enumerate(objects, start=1):
        obj_bytes = f"{object_number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        offsets.append(current_offset)
        pdf.append(obj_bytes)
        current_offset += len(obj_bytes)

    xref_offset = current_offset
    xref = [f"xref\n0 {len(objects) + 1}\n".encode("ascii"), b"0000000000 65535 f \n"]
    for offset in offsets[1:]:
        xref.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    xref.append(
        (
            "trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n"
        ).format(size=len(objects) + 1, startxref=xref_offset).encode("ascii")
    )

    pdf.extend(xref)
    return b"".join(pdf)


def _build_content_stream(title: str, subtitle: str | None, lines: list[str], page_number: int, page_total: int) -> bytes:
    commands = [
        "BT",
        "/F1 16 Tf",
        f"{LEFT_MARGIN} {PAGE_HEIGHT - TOP_MARGIN} Td",
        f"({_escape_pdf_text(title)}) Tj",
    ]
    if subtitle:
        commands.extend(
            [
                "/F1 10 Tf",
                f"0 -18 Td",
                f"({_escape_pdf_text(subtitle)}) Tj",
            ]
        )

    commands.extend(
        [
            f"/F1 {BODY_SIZE} Tf",
            f"0 -26 Td",
        ]
    )

    for line in lines:
        commands.append(f"({_escape_pdf_text(line)}) Tj")
        commands.append(f"0 -{LINE_HEIGHT} Td")

    commands.extend(
        [
            "/F1 9 Tf",
            f"0 -10 Td",
            f"(Page {page_number} of {page_total}) Tj",
            "ET",
        ]
    )
    return "\n".join(commands).encode("latin-1", errors="replace")


def build_pdf_document(title: str, lines: list[str], subtitle: str | None = None) -> bytes:
    usable_height = PAGE_HEIGHT - TOP_MARGIN - BOTTOM_MARGIN - 70
    lines_per_page = max(1, usable_height // LINE_HEIGHT)
    total_pages = max(1, ceil(len(lines) / lines_per_page))
    chunks = [lines[index : index + lines_per_page] for index in range(0, len(lines), lines_per_page)]
    if not chunks:
        chunks = [[]]

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")

    # Placeholder for the pages tree. The kids array is filled after page objects are known.
    pages_index = len(objects) + 1
    objects.append(b"")

    font_object_id = len(objects) + 1
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    page_object_ids: list[int] = []
    content_object_ids: list[int] = []

    for page_number, chunk in enumerate(chunks, start=1):
        content_object_ids.append(len(objects) + 1)
        content = _build_content_stream(title, subtitle, chunk, page_number, total_pages)
        objects.append(b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream")

        page_object_ids.append(len(objects) + 1)
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] "
                "/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>"
            ).format(w=PAGE_WIDTH, h=PAGE_HEIGHT, font=font_object_id, content=content_object_ids[-1]).encode("ascii")
        )

    kids = " ".join(f"{page_id} 0 R" for page_id in page_object_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_object_ids)} >>".encode("ascii")

    pdf = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets = [0]
    current_offset = len(pdf[0])

    for object_number, body in enumerate(objects, start=1):
        obj_bytes = f"{object_number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        offsets.append(current_offset)
        pdf.append(obj_bytes)
        current_offset += len(obj_bytes)

    xref_offset = current_offset
    xref = [f"xref\n0 {len(objects) + 1}\n".encode("ascii"), b"0000000000 65535 f \n"]
    for offset in offsets[1:]:
        xref.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    xref.append(
        (
            "trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n"
        ).format(size=len(objects) + 1, startxref=xref_offset).encode("ascii")
    )

    pdf.extend(xref)
    return b"".join(pdf)
