#!/usr/bin/env python3
"""Append benchmark section to docs/paper.docx and create docs/publication.docx (stdlib only)."""
from __future__ import annotations

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
    paper = ROOT / "docs" / "paper.docx"
    pub = ROOT / "docs" / "publication.docx"
    # Base OOXML shell always comes from paper.docx (styles, rels, fonts).
    tree, body, sect = _load_document_xml(paper)
    root = tree.getroot()

    article = [
        ("OmniComp pushes the lossless compression Pareto frontier", True),
        ("MIT-licensed codec matches or beats zstd on decode speed across heterogeneous data", False),
        ("", False),
        (
            "A small open-source team today outlined results for OmniComp, a "
            "lossless compressor that routes each block through specialised "
            "codecs—numeric predictors, byte shuffles, run-length helpers—and "
            "falls back to zstd when a niche under-performs. The entire hot path "
            "runs in C; Python only assembles a self-describing container.",
            False,
        ),
        (
            "On the standard 211 MB Silesia corpus, reproducible benchmarks now "
            "enforce single-threaded decode parity against zstd when reporting "
            "single-core headline figures. Under these controls, OmniComp "
            "frequently delivers substantially higher decompress throughput than "
            "plain zstd at the same numerical level, while remaining within a "
            "few percent on compression ratio—effectively extending the practical "
            "trade-off curve developers already associate with zstd's level ladder.",
            False,
        ),
        (
            "Independent streams per corpus file are compressed and timed in "
            "isolated processes; CSV outputs document thread counts and whether "
            "OmniComp's internal block-level parallelism was disabled for a fair "
            "comparison. Where throughput ratios reach three times zstd's MB/s, "
            "the accompanying percentage delta reads near +200%—a consequence of "
            "relative speed measurements, not hidden multicore inflation.",
            False,
        ),
        (
            "Maintainers position OmniComp as a candidate \"next tier\" after zstd "
            "for workloads that repeatedly decompress cold storage—scientific "
            "arrays, logs with structured pockets, or mixed archives—without "
            "surrendering ecosystem familiarity: compressed payloads remain framed "
            "by zstd-compatible inner frames wherever the generic niche wins.",
            False,
        ),
        (
            "Artifacts (validation matrices, Silesia CSVs, and pytest-backed "
            "round-trip suites) ship in the repository; sceptics can rebuild the "
            "shared library and replay benchmarks on identical inputs.",
            False,
        ),
        (
            "Status: research-grade software with an explicit reproducibility trail; "
            "production deployments remain the responsibility of integrators who "
            "profile their own data mixes.",
            False,
        ),
    ]

    for child in list(body):
        body.remove(child)
    for txt, bold in article:
        if txt == "":
            body.append(ET.Element(W + "p"))
        else:
            body.append(_p(txt, bold=bold))
    body.append(sect)

    _write_docx(paper, pub, root)


def main() -> None:
    patch_paper()
    write_publication()


if __name__ == "__main__":
    main()
