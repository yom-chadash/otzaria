"""Record the pinned Flutter SDK and verified Linux PDFium producer bytes."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import tarfile

URL = "https://github.com/bblanchon/pdfium-binaries/releases/download/chromium%2F8125/pdfium-linux-x64.tgz"
ARCHIVE_SHA256 = "e1c3ce6e270cb5e0c20816b379ad9f2c1309972697c505f508de92bf5e7d6659"
BINARY_SHA256 = "1c251efce460d67437a4c76d4be9033ae2f17bf5603f5bff1af10209b94ca723"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("This producer artifact is Linux x64 only.")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    raw = subprocess.check_output(["flutter", "--version", "--machine"], text=True, encoding="utf-8")
    sdk = json.loads(raw)
    if (sdk.get("frameworkVersion") != "3.47.7"
            or sdk.get("frameworkRevision") != "abaf9c523780a608bd46686fd5e53740a07077f8"
            or sdk.get("engineRevision") != "deb287481e3ce9468f3434937ced4240a70539ca"
            or not sdk.get("dartSdkVersion", "").startswith("3.13.5")):
        raise ValueError("The experiment requires Flutter 3.47.7.")
    (output / "flutter.json").write_text(raw, encoding="utf-8")
    archive, binary = output / "pdfium.tgz", output / "libpdfium.so"
    subprocess.run(["curl", "--fail", "--location", "--retry", "3", "--max-time", "300",
                    URL, "--output", str(archive)], check=True)
    if digest(archive) != ARCHIVE_SHA256:
        raise ValueError("PDFium producer archive digest mismatch.")
    with tarfile.open(archive, "r:gz") as reader:
        member = reader.getmember("lib/libpdfium.so")
        if not member.isfile():
            raise ValueError("The verified archive does not contain a regular PDFium image.")
        with reader.extractfile(member) as source, binary.open("xb") as destination:
            while chunk := source.read(1024 * 1024):
                destination.write(chunk)
    if digest(binary) != BINARY_SHA256:
        raise ValueError("PDFium native binary digest mismatch.")
    (output / "producer.json").write_text(json.dumps({
        "schemaVersion": 1, "sourceUrl": URL, "archiveSha256": ARCHIVE_SHA256,
        "binarySha256": BINARY_SHA256, "binaryBytes": binary.stat().st_size,
        "platform": "linux-x64", "producer": "bblanchon/pdfium-binaries chromium/8125",
    }, indent=2) + "\n", encoding="utf-8")
    print(f"Verified Flutter 3.47.7 and PDFium {BINARY_SHA256}.")


if __name__ == "__main__":
    main()
