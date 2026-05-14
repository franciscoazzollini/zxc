#!/usr/bin/env python3
"""Append benchmark section to docs/paper.docx.

Regenerates ``docs/publication.docx`` via ``scripts/build_publication_docx.py``
(requires the ``python-docx`` package; use ``uv run --with python-docx`` if needed).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

ROOT = Path(__file__).resolve().parents[1]


def _p(text: str, *, bold: bool = False) -> ET.Element:
    p = ET.Element(W + "p")
    r = ET.SubElement(p, W + "r")
    if bold:
        rpr = ET.SubElement(r, W + "rPr")
        ET.SubElement(rpr, W + "b")
        ET.SubElement(rpr, W + "bCs")
    t = ET.SubElement(r, W + "t")
    t.text = text
    return p


def _load_document_xml(zpath: Path) -> tuple[ET.ElementTree, ET.Element, ET.Element]:
    with zipfile.ZipFile(zpath, "r") as zf:
        raw = zf.read("word/document.xml")
    root = ET.fromstring(raw)
    body = root.find(f".//{W}body")
    if body is None:
        raise RuntimeError("no w:body")
    sect = body.find(W + "sectPr")
    if sect is None:
        raise RuntimeError("no w:sectPr")
    return ET.ElementTree(root), body, sect


def _write_docx(src_template: Path, dst: Path, root: ET.Element) -> None:
    import io

    buffer = io.BytesIO()
    ET.ElementTree(root).write(buffer, encoding="utf-8", xml_declaration=True)
    out_xml = buffer.getvalue()

    out_zip = io.BytesIO()
    with zipfile.ZipFile(src_template, "r") as zin:
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "word/document.xml":
                    data = out_xml
                zout.writestr(item, data)
    dst.write_bytes(out_zip.getvalue())


def patch_paper() -> None:
    paper = ROOT / "docs" / "paper.docx"
    tree, body, sect = _load_document_xml(paper)
    with zipfile.ZipFile(paper, "r") as zf:
        if b"Benchmark integrity (single-thread decode parity)" in zf.read(
            "word/document.xml"
        ):
            return  # already patched
    idx = list(body).index(sect)

    blocks = [
        ("Benchmark integrity (single-thread decode parity)", True),
        (
            "Per-file Silesia benchmarks now pin OmniComp's OMN8 block decoder to "
            "sequential pthread-off mode whenever n_threads = 1 "
            "(environment variable OMNICOMP_OMN8_DECOMP_SINGLE_THREAD=1), matching "
            "libzstd's single-threaded decompress path. Without this guard, "
            "multi-block parallel decode inside one container could inflate "
            "decompress MB/s versus zstd on large streams.",
            False,
        ),
        (
            "Committed CSV artifacts record n_threads, omni_seq_block_decode "
            "(1 when sequential decode is active for the Omni worker), "
            "decomp_mb_s_ratio_omni_over_zstd (direct Omni/zstd throughput ratio), "
            "and the usual percentage deltas. Large positive deltas reflect "
            "multiplicative throughput gains (for example ratio ≈ 3 corresponds "
            "to roughly +200% in the percentage column).",
            False,
        ),
        (
            "Blob rows aggregate independent streams per corpus file with explicit "
            "parallel decompress pools only where documented; headline numbers for "
            "developer messaging should cite per-file rows when discussing strict "
            "single-thread parity.",
            False,
        ),
    ]

    for i, (txt, bold) in enumerate(blocks):
        body.insert(idx + i, _p(txt, bold=bold))

    _write_docx(paper, paper, tree.getroot())


def write_publication() -> None:
    """Rebuild ``docs/publication.docx`` (Spanish Medium-style article)."""
    script = ROOT / "scripts" / "build_publication_docx.py"
    if shutil.which("uv"):
        subprocess.run(
            ["uv", "run", "--with", "python-docx", "python", str(script)],
            cwd=ROOT,
            check=True,
        )
    else:
        subprocess.run([sys.executable, str(script)], cwd=ROOT, check=True)


def main() -> None:
    patch_paper()
    write_publication()


if __name__ == "__main__":
    main()
