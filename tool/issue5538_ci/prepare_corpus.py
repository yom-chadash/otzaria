"""Fetch only pinned public PDFs and describe the exact shared CI corpus."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request
from urllib.parse import urlparse

import pypdf

PYPDF_VERSION = "6.21.4"
CATALOG = Path(__file__).with_name("corpus_sources.json")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_catalog(value):
    if (not isinstance(value, dict) or value.get("schemaVersion") != 1
            or not isinstance(value.get("documents"), list) or not value["documents"]):
        raise ValueError("Invalid corpus catalog.")
    seen = set()
    for item in value["documents"]:
        identifier = item.get("id")
        if (not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", identifier)
                or identifier in seen):
            raise ValueError("Duplicate or unsafe corpus identifier.")
        seen.add(identifier)
        if not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]):
            raise ValueError("Every corpus source must be pinned by SHA-256 before CI.")
        for key in ("sizeBytes", "pageCount"):
            if type(item.get(key)) is not int or item[key] < 1:
                raise ValueError(f"Invalid corpus {key}.")
        for key in ("sourceUrl", "licenseUrl"):
            if urlparse(item.get(key, "")).scheme != "https":
                raise ValueError("Corpus and license URLs must use HTTPS.")
        for key in ("title", "attribution", "license", "classification"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"Missing corpus provenance: {key}.")
    return value


def verify_bytes(path, item):
    if path.is_symlink() or not path.is_file():
        raise ValueError("A corpus PDF must be a regular file, not a symlink.")
    if path.stat().st_size != item["sizeBytes"] or digest(path) != item["sha256"]:
        raise ValueError(f"Corpus bytes differ from the reviewed pin: {item['id']}.")
    with path.open("rb") as stream:
        if b"%PDF-" not in stream.read(1024):
            raise ValueError("The downloaded content is not a PDF.")


def download(item, destination, input_directory=None):
    if input_directory:
        source = input_directory / f"{item['id']}.pdf"
        verify_bytes(source, item)
        shutil.copyfile(source, destination)
        verify_bytes(destination, item)
        return {"method": "verified-local-copy"}
    request = urllib.request.Request(item["sourceUrl"], headers={
        "User-Agent": "Otzaria issue5538 reproducible public PDF corpus (GitHub Actions)",
    })
    partial = destination.with_suffix(".partial")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=120) as response, partial.open("xb") as output:
                if urlparse(response.url).scheme != "https":
                    raise ValueError("A corpus download redirected outside HTTPS.")
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > item["sizeBytes"]:
                        raise ValueError("The response exceeded the pinned PDF size.")
                    output.write(chunk)
                provenance = {"method": "https", "finalUrl": response.url,
                    "etag": response.headers.get("ETag"), "lastModified": response.headers.get("Last-Modified")}
            verify_bytes(partial, item)
            partial.rename(destination)
            return provenance
        except Exception:
            partial.unlink(missing_ok=True)
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("Unreachable download state.")


def font_records(resources, visited=None):
    visited = visited if visited is not None else set()
    resources = resources.get_object() if resources else {}
    result = set()
    for font in resources.get("/Font", {}).get_object().values() if "/Font" in resources else ():
        font = font.get_object()
        descriptors = [font.get("/FontDescriptor")]
        descendants = font.get("/DescendantFonts", [])
        descriptors.extend(child.get_object().get("/FontDescriptor") for child in descendants)
        embedded = any(descriptor and any(key in descriptor.get_object() for key in
                        ("/FontFile", "/FontFile2", "/FontFile3")) for descriptor in descriptors)
        result.add((str(font.get("/BaseFont", "")), str(font.get("/Subtype", "")), bool(embedded)))
    xobjects = resources.get("/XObject", {})
    xobjects = xobjects.get_object() if xobjects else {}
    for reference in xobjects.values():
        key = (getattr(reference, "idnum", None), getattr(reference, "generation", None))
        if key == (None, None):
            key = id(reference)
        if key in visited:
            continue
        visited.add(key)
        obj = reference.get_object()
        if obj.get("/Subtype") == "/Form":
            result.update(font_records(obj.get("/Resources"), visited))
    return result


def describe_pdf(path):
    reader = pypdf.PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("The reviewed corpus must be readable without credentials.")
    sizes, fonts = Counter(), set()
    total_chars = hebrew_chars = marks = pages_with_text = links = 0
    text_hash = hashlib.sha256()
    page_text = []
    resource_cache = {}
    def reference_key(value):
        reference = getattr(value, "indirect_reference", value)
        if hasattr(reference, "idnum"):
            return ("reference", reference.idnum, reference.generation)
        if isinstance(value, dict):
            entries = tuple(sorted((str(key), reference_key(item)) for key, item in value.items()))
            if hasattr(value, "get_data"):
                return ("stream", entries, hashlib.sha256(value.get_data()).digest())
            return ("dictionary", entries)
        if isinstance(value, list):
            return tuple(reference_key(item) for item in value)
        if value is None or isinstance(value, (str, bytes, int, float, bool)):
            return (type(value).__name__, value)
        return ("direct-object", id(value))
    for number, page in enumerate(reader.pages, start=1):
        box = page.cropbox
        sizes[(round(float(box.width), 4), round(float(box.height), 4))] += 1
        # Repeated stress pages share the same immutable content/resources.
        # Inspect those exact objects once instead of parsing the same OCR
        # stream 27 times; still retain a hash and count for every output page.
        contents = page.get_contents()
        key = (hashlib.sha256(contents.get_data() if contents else b"").digest(),
               reference_key(page.get("/Resources")), page.get("/Rotate", 0))
        if key not in resource_cache:
            text = page.extract_text() or ""
            encoded = text.encode("utf-8")
            resource_cache[key] = {
                "encoded": encoded, "characters": len(text), "sha256": hashlib.sha256(encoded).hexdigest(),
                "hebrewLetters": sum("\u05d0" <= c <= "\u05ea" for c in text),
                "hebrewMarks": sum("\u0591" <= c <= "\u05bd" or c in "\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7" for c in text),
                "hasText": bool(text.strip()), "fonts": font_records(page.get("/Resources")),
            }
        observed = resource_cache[key]
        encoded = observed["encoded"]
        text_hash.update(len(encoded).to_bytes(8, "big"))
        text_hash.update(encoded)
        total_chars += observed["characters"]
        hebrew_chars += observed["hebrewLetters"]
        marks += observed["hebrewMarks"]
        pages_with_text += observed["hasText"]
        page_text.append({"page": number, "characters": observed["characters"], "sha256": observed["sha256"]})
        fonts.update(observed["fonts"])
        links += sum(annotation.get_object().get("/Subtype") == "/Link" for annotation in page.get("/Annots", []))
    return {
        "pageCount": len(reader.pages), "pageSizePoints": [
            {"width": width, "height": height, "pages": count} for (width, height), count in sorted(sizes.items())],
        "fontResources": [{"baseFont": name, "subtype": subtype, "embedded": embedded}
                          for name, subtype, embedded in sorted(fonts)],
        "linkAnnotations": links,
        "textInspection": {"extractor": f"pypdf {pypdf.__version__}", "pagesWithExtractedText": pages_with_text,
            "characters": total_chars, "hebrewLetters": hebrew_chars, "hebrewMarks": marks,
            "distinctContentResourcePairs": len(resource_cache),
            "lengthPrefixedUtf8Sha256": text_hash.hexdigest(), "pages": page_text,
            "limitation": "This inspection identifies corpus content; PDFium parity is measured separately."},
    }


def derive_repeated_pages(source, target, page_count):
    reader = pypdf.PdfReader(source)
    writer = pypdf.PdfWriter()
    for index in range(page_count):
        writer.add_page(reader.pages[index % len(reader.pages)])
    writer.add_metadata({
        "/Title": "Otzaria issue5538 derived real-content stress fixture",
        "/Subject": f"Repeated public-domain source pages, not a {page_count}-page original book. Source SHA256 {digest(source)}",
    })
    with target.open("xb") as stream:
        writer.write(stream)
    writer.close()


def prepare(args):
    if pypdf.__version__ != PYPDF_VERSION:
        raise ValueError(f"Corpus inspection requires pypdf {PYPDF_VERSION}.")
    catalog = validate_catalog(json.loads(args.catalog.read_text(encoding="utf-8")))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cases = output / "cases"
    cases.mkdir()
    receipt = {"schemaVersion": 1, "status": "running", "catalogSha256": digest(args.catalog),
               "inspector": {"pypdf": pypdf.__version__}, "documents": [], "generatedCases": []}
    try:
        for item in catalog["documents"]:
            path = output / f"{item['id']}.pdf"
            provenance = download(item, path, args.input_directory)
            description = describe_pdf(path)
            if description["pageCount"] != item["pageCount"]:
                raise ValueError("The pinned PDF has an unexpected page count.")
            record = {**item, "path": path.name, "download": provenance, **description}
            receipt["documents"].append(record)
            write_json(cases / f"{item['id']}.json", {"schemaVersion": 1, "source": {
                "kind": "external", "path": f"../{path.name}", "sha256": item["sha256"],
                "pageCount": item["pageCount"]}})
            write_json(output / "corpus.json", receipt)
            print(f"Verified {item['id']}: {item['pageCount']} pages, {item['sha256']}", flush=True)
        for count in (10, 100, 1000, 10000):
            identifier = f"synthetic-mixed-{count}"
            write_json(cases / f"{identifier}.json", {"schemaVersion": 1, "source": {
                "kind": "generated", "pageCount": count, "mixedPageSizes": True}})
            receipt["generatedCases"].append({"id": identifier, "pageCount": count,
                "classification": "synthetic mixed-size stress fixture; not a real book"})
        original = next(item for item in receipt["documents"] if item["id"] == "mikraot-gedolot-genesis-1859")
        identifier = "derived-real-mixed-10000"
        target = output / f"{identifier}.pdf"
        derive_repeated_pages(output / original["path"], target, 10000)
        description = describe_pdf(target)
        expected_sizes = Counter()
        source_reader = pypdf.PdfReader(output / original["path"])
        for index in range(10000):
            box = source_reader.pages[index % len(source_reader.pages)].cropbox
            expected_sizes[(round(float(box.width), 4), round(float(box.height), 4))] += 1
        actual_sizes = {(row["width"], row["height"]): row["pages"] for row in description["pageSizePoints"]}
        source_text = original["textInspection"]["pages"]
        if (description["pageCount"] != 10000 or actual_sizes != dict(expected_sizes)
                or any(page["sha256"] != source_text[index % len(source_text)]["sha256"]
                       for index, page in enumerate(description["textInspection"]["pages"]))):
            raise ValueError("Derived stress corpus has incorrect page geometry or count.")
        derived_hash = digest(target)
        receipt["documents"].append({**original, **description, "id": identifier, "path": target.name,
            "title": "10,000 repeated real scan pages, mixed-size stress fixture",
            "sha256": derived_hash, "sizeBytes": target.stat().st_size,
            "classification": "derived-real-content stress; repeated pages, not an original 10,000-page book",
            "derivation": {"tool": f"pypdf {pypdf.__version__}", "sourceSha256": original["sha256"],
                "sourcePages": original["pageCount"], "pageMapping": "1 + zeroBasedOutputPage % sourcePages"},
            "download": {"method": "local-lossless-page-repeat"}})
        write_json(cases / f"{identifier}.json", {"schemaVersion": 1, "source": {
            "kind": "external", "path": f"../{target.name}", "sha256": derived_hash, "pageCount": 10000}})
        write_json(output / "corpus.json", receipt)
        if args.qpdf:
            original = next(item for item in receipt["documents"] if item["id"] == "gcc-15_2")
            target = output / "gcc-15_2-linearized.pdf"
            version = subprocess.check_output([args.qpdf, "--version"], text=True).strip()
            subprocess.run([args.qpdf, "--linearize", str(output / original["path"]), str(target)], check=True)
            description = describe_pdf(target)
            if (description["pageCount"] != original["pageCount"]
                    or description["textInspection"]["lengthPrefixedUtf8Sha256"] !=
                       original["textInspection"]["lengthPrefixedUtf8Sha256"]):
                raise ValueError("Linearization changed the document page count or extracted text.")
            receipt["documents"].append({**original, **description, "id": "gcc-15_2-linearized",
                "path": target.name, "sha256": digest(target), "sizeBytes": target.stat().st_size,
                "classification": "derived real document: qpdf linearization, unchanged text and pages",
                "derivation": {"tool": version, "arguments": ["--linearize"], "sourceSha256": original["sha256"]},
                "download": {"method": "local-qpdf-linearization"}})
        for record in receipt["documents"]:
            verify_bytes(output / record["path"], record)
        receipt["status"] = "passed"
    except BaseException as error:
        receipt.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        write_json(output / "corpus.json", receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input-directory", type=Path)
    parser.add_argument("--qpdf")
    prepare(parser.parse_args())


if __name__ == "__main__":
    main()
