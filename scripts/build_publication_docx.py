#!/usr/bin/env python3
"""Regenerate docs/publication.docx for Medium-style outreach (Spanish).

Requires: python-docx (``uv run --with python-docx python scripts/build_publication_docx.py``).
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt


def _italic_lede(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.italic = True
    r.font.size = Pt(12)


def _h(doc: Document, text: str, level: int = 1) -> None:
    doc.add_heading(text, level=level)


def _p(doc: Document, text: str) -> None:
    doc.add_paragraph(text)


def build() -> Document:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Georgia"
    style.font.size = Pt(11)

    title = doc.add_heading(
        "Más allá de zstd: un compresor sin pérdidas que redibuja la frontera de Pareto",
        level=0,
    )
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    _italic_lede(
        doc,
        "OmniComp es código abierto (MIT). En datos heterogéneos reales —el corpus "
        "Silesia— suele decodificar más rápido que Zstandard al mismo nivel numérico, "
        "manteniendo el ratio dentro de unos pocos puntos porcentuales. Lo que sigue "
        "no es marketing: es arquitectura, metodología y artefactos reproducibles.",
    )
    doc.add_paragraph()

    _h(doc, "1. El problema que ya creíamos resuelto")
    _p(
        doc,
        "Durante años, Zstandard (zstd) se ha convertido en la referencia práctica "
        "para compresión sin pérdidas rápida y predecible. Su escalera de niveles "
        "(1…22) es un mapa mental compartido: subes el nivel, ganas ratio y pagas "
        "tiempo de CPU. Esa claridad es oro en ingeniería.",
    )
    _p(
        doc,
        "Pero los datos reales no son uniformes. Un archivo puede mezclar texto, "
        "cabeceras binarias, tablas de enteros, floats correlacionados y tramos casi "
        "aleatorios. Un solo códec genérico excelente aplica la misma familia de "
        "ideas a todo el flujo. La pregunta incómoda es: ¿dejamos rendimiento sobre "
        "la mesa porque no reorganizamos el problema antes de comprimir?",
    )

    _h(doc, "2. La idea: adaptarse por bloques, sin romper el ecosistema")
    _p(
        doc,
        "OmniComp no sustituye la matemática de zstd como último recurso. "
        "Particiona la entrada en bloques, clasifica cada uno en un «nicho» "
        "(texto, shuffle por stride, predictores para floats, constantes, ruido, "
        "etc.) y elige un códec especializado cuando tiene sentido. El camino caliente "
        "está en C; Python solo monta un contenedor autodescriptivo (OMN8).",
    )
    _p(
        doc,
        "La pieza que evita fantasías es la cascada competitiva: si la ruta "
        "especializada no gana en tamaño frente a zstd «plano» sobre los mismos bytes "
        "originales, el formato se queda con zstd. Es decir: el sistema es "
        "conservador. No apuesta la integridad del ratio a una heurística dudosa.",
    )

    _h(doc, "3. Cómo evitamos inflar los números (y por qué importa)")
    _p(
        doc,
        "En cargas de descompresión, comparar un decodificador multihilo contra "
        "libzstd en un solo hilo sería regalar la narrativa. Por eso los CSV "
        "publicados en el repositorio documentan, entre otras cosas, si OmniComp "
        "desactivó el paralelismo interno entre bloques (omni_seq_block_decode = 1) "
        "para equiparar contra descompresión single-thread de libzstd.",
    )
    _p(
        doc,
        "Cuando el ratio de throughput Omni/zstd llega a 3×, el delta porcentual "
        "respecto a zstd ronda +200 %. Eso no es «magia multicore oculta»: es la "
        "definición de comparar dos velocidades relativas. Las tablas también "
        "registran hilos de compresión y el estado de la corrida para que un "
        "escéptico pueda reproducir.",
    )

    _h(doc, "4. Resultados que puedes volver a medir (Silesia, un hilo)")
    _p(
        doc,
        "Silesia (~202 MB) sigue siendo el laboratorio de cabecera para "
        "heterogeneidad. En configuración de referencia (un hilo, paridad de decode "
        "documentada), OmniComp a menudo se sitúa a pocos puntos porcentuales del "
        "ratio de zstd al mismo nivel nominal, con descompresión claramente más "
        "rápida en muchas filas del corpus.",
    )
    _p(
        doc,
        "Ejemplos orientativos del resumen blob (hardware Apple Silicon, zstd 1.5.6, "
        "mejor de tres ejecuciones con examples/benchmark_silesia.py): a nivel 9, "
        "OmniComp alcanza un ratio similar a zstd -9 con alrededor de +18 % de "
        "throughput de descompresión; a nivel 12, se sitúa en territorio de ratio "
        "tipo zstd -15 con compresión del orden de ~5× más rápida en pared; a nivel "
        "22, entra en la zona de ratio de zstd -22 con ~47 % menos tiempo de "
        "compresión y mejora de decode modesta pero real.",
    )

    _h(doc, "5. Donde el diseño se nota: datos con estructura de stride")
    _p(
        doc,
        "En el fichero x-ray de Silesia (imagen médica 16-bit, ~8 MB), OmniComp "
        "elige automáticamente byte-shuffle de 8 bytes: separa el flujo en ocho "
        "sub-bandas que zstd comprime mucho mejor. El resultado publicado en el "
        "repositorio es del orden de +25 % de ratio frente a zstd -3 y alrededor "
        "de +80 % de throughput de descompresión frente al mismo punto de "
        "comparación —porque invertir un shuffle bien definido tras zstd es barato.",
    )

    _h(doc, "6. Honestidad radical: qué no estamos prometiendo")
    _p(
        doc,
        "OmniComp no es un reemplazo universal para zstd en todos los KPIs. En "
        "ajustes ultra-rápidos (niveles 1–2), zstd sigue siendo fuerte en ratio "
        "genérico. En blobs casi enteramente «genéricos», la mayor parte del tiempo "
        "sigue yendo a tramas zstd internas: los márgenes se achican. Producción "
        "real exige perfilar tu mezcla de datos, versionar el formato y asumir la "
        "operación como integrador.",
    )
    _p(
        doc,
        "Esto es software de grado investigación con un sendero explícito de "
        "reproducibilidad: matrices de validación, CSVs de Silesia, tests de "
        "round-trip con pytest. Lo que no es: una promesa de certificación para "
        "cada despliegue imaginable.",
    )

    _h(doc, "7. Licencias, pie de upstream y cómo probarlo hoy")
    _p(
        doc,
        "OmniComp se publica bajo licencia MIT. El pipeline usa la biblioteca "
        "Zstandard de Meta (BSD / GPLv2 dual en origen); en el build por defecto del "
        "repositorio, zstd se compila desde el submódulo third_party/zstd y enlaza "
        "como biblioteca estática dentro de libomnicomp_pipeline, sin depender de "
        "libzstd en tiempo de ejecución del sistema. Los términos exactos de zstd "
        "siguen aplicando al código de Meta —véase NOTICE en el repositorio.",
    )
    _p(
        doc,
        "Para reproducir: clona el repo, inicializa submódulos, ejecuta make, "
        "instala el paquete Python en editable y lanza examples/benchmark_silesia.py "
        "con los mismos flags que documentan los CSV. También hay un CLI zxc para "
        "comprimir archivos sueltos desde terminal tras pip install -e .",
    )

    _h(doc, "8. Cierre: un posible «siguiente peldaño»")
    _p(
        doc,
        "Si tu trabajo vuelve una y otra vez a descomprimir almacenamiento frío —"
        "arrays científicos, logs con bolsillos estructurados, archivos mezclados— "
        "OmniComp propone un peldaño adicional después de zstd: misma escalera "
        "mental de niveles, curva Pareto práctica ampliada, y un compromiso explícito "
        "con mediciones que no confunden paralelismo con innovación.",
    )
    _p(
        doc,
        "El criterio final no es este documento: es tu propia réplica del "
        "benchmark y tus datos. Si al reproducir encuentras un fallo o una "
        "oportunidad de mejora, el proyecto gana con issues concretos y parches "
        "pequeños. Esa es la barra de un avance que merece la palabra «breakthrough» "
        "en Medium: no ruido, sino trazabilidad.",
    )

    doc.add_paragraph()
    sig = doc.add_paragraph()
    sig.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = sig.add_run(
        "Texto de divulgación generado para docs/publication.docx — "
        "regenerable con scripts/build_publication_docx.py"
    )
    r.italic = True
    r.font.size = Pt(9)

    return doc


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    out = root / "docs" / "publication.docx"
    doc = build()
    doc.save(out)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
