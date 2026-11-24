"""Deterministic, unembedded-font inputs for the fork-only S02 app campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


SCENARIOS = (
    "initial-hebrew", "initial-latin", "shared-fonts", "different-fonts",
    "late-page", "late-text", "close-pending-font", "replace-pending-font",
)


def pdf(face: str | None, *, hebrew: bool = True, late: bool = False) -> bytes:
    """A tiny syntactically complete PDF; widths intentionally are not fixed."""
    pages = 3 if late else 1
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", ""]
    font_id = 3 + pages * 2
    for page in range(pages):
        page_id = 3 + page * 2
        has_text = face is not None and page == pages - 1
        content = ""
        if has_text:
            content = "BT /F1 36 Tf\n"
            if hebrew:
                codes = [ord(char) - 0x5D0 + 1 for char in reversed("שלום")]
                encoded = "".join(f"\\{code:03o}" for code in codes)
                content += f"1 0 0 1 120 240 Tm ({encoded}) Tj\n"
            else:
                content += "1 0 0 1 80 240 Tm (Hello) Tj\n"
            content += "ET\n"
            content += "BT /F2 9 Tf 1 0 0 1 24 24 Tm (Standard14 control) Tj ET\n"
        resources = f"/Font << /F1 {font_id} 0 R /F2 {font_id + 2} 0 R >>" if has_text else ""
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 320 400] "
            f"/Contents {page_id + 1} 0 R /Resources << {resources} >> >>"
        )
        objects.append(f"<< /Length {len(content)} >>\nstream\n{content}endstream")
    kids = " ".join(f"{3 + page * 2} 0 R" for page in range(pages))
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>"
    if face is not None:
        differences = " ".join(f"/uni{0x5d0 + index:04X}" for index in range(27))
        encoding = (
            f"<< /Type /Encoding /BaseEncoding /WinAnsiEncoding /Differences [1 {differences}] >>"
            if hebrew else "/WinAnsiEncoding"
        )
        widths = " ".join(str(350 + (index % 7) * 45) for index in range(256))
        objects.append(
            f"<< /Type /Font /Subtype /TrueType /BaseFont /{face} /FirstChar 0 "
            f"/LastChar 255 /Widths [{widths}] /Encoding {encoding} "
            f"/FontDescriptor {font_id + 1} 0 R >>"
        )
        objects.append(
            f"<< /Type /FontDescriptor /FontName /{face} /Flags 32 "
            "/FontBBox [0 -250 1000 850] /ItalicAngle 0 /Ascent 750 "
            "/Descent -250 /CapHeight 700 /StemV 80 >>"
        )
        objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output += f"{index} 0 obj\n{obj}\nendobj\n".encode("ascii")
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode("ascii")
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(output)


def prepare(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    cases = []
    for scenario in SCENARIOS:
        folder = output / scenario
        folder.mkdir()
        count = 2 if scenario in ("shared-fonts", "different-fonts") else 1
        documents = []
        for index in range(count):
            face_suffix = "shared" if scenario == "shared-fonts" else str(index)
            face = "OtzariaSZeroTwo" + scenario.replace("-", "") + face_suffix
            path = folder / f"source-{index}.pdf"
            late = scenario in ("late-page", "late-text")
            hebrew = scenario != "initial-latin"
            data = pdf(face, hebrew=hebrew, late=late)
            path.write_bytes(data)
            documents.append({
                "id": f"source-{index}", "path": str(path.relative_to(output)),
                "sha256": hashlib.sha256(data).hexdigest(), "sizeBytes": len(data),
                "pageCount": 3 if late else 1, "targetPage": 3 if late else 1,
                "requestedFace": face, "expectedWord": "שלום" if hebrew else "Hello",
                "fontResourcesByPage": {str(3 if late else 1): [face, "Helvetica"]},
                "expectedControlText": "Standard14 control",
            })
        # A font-free peer proves that recovery does not reopen unrelated books.
        blank = folder / "unrelated.pdf"
        data = pdf(None)
        blank.write_bytes(data)
        documents.append({
            "id": "unrelated", "path": str(blank.relative_to(output)),
            "sha256": hashlib.sha256(data).hexdigest(), "sizeBytes": len(data),
            "pageCount": 1, "targetPage": 1, "requestedFace": None, "expectedWord": "",
            "fontResourcesByPage": {}, "expectedControlText": "",
        })
        cases.append({"id": scenario, "documents": documents})
    manifest = {
        "schemaVersion": 1, "status": "prepared", "cases": cases,
        "scope": "Synthetic unembedded TrueType fixtures, not a real-book corpus.",
        "fontPolicy": "Unchanged app platform/system/bundled policy. Log actual native charset and pitch; do not infer Hebrew charset from these PDF bytes.",
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    prepare(parser.parse_args().output.resolve())
