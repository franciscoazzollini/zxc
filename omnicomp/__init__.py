"""OmniComp v8 TURBO: lossless adaptive multi-niche compressor.

Public API:
    compress(data, level=3, n_threads=0, block_size=0) -> bytes
    decompress(comp) -> bytes

The ``level`` knob (1..22, default 3) controls the speed/ratio trade-off.
It is mapped to a per-block strategy inside the C pipeline:

    level <= 1   - "fast"   : no cascade, zstd-1 everywhere
    level <= 3   - default  : smart cascade, zstd-1 inside specialised codecs
    level <= 6   - balanced : always cascade, codec internal zstd-2
    level >= 7   - "strong" : always cascade, codec internal zstd-3,
                              comp / cascade at the user's level (up to 22)

Decompression is identical regardless of the level used at compression
time; the container is self-describing.

Lower-level aliases (kept for backwards compatibility):
    omni_compress_v8_turbo(...) -> bytes
    omni_decompress_v8(...)     -> bytes
"""

from ._omnicomp import (
    omni_compress_v8_turbo,
    omni_decompress_v8,
    NICHE_NAMES,
    MAGIC_V8,
)


def compress(data, level: int = 3, n_threads: int = 0,
             block_size: int = 0) -> bytes:
    """Compress ``data`` with OmniComp.

    Args:
        data:        bytes-like to compress.
        level:       1..22. Higher = better ratio, slower. Default 3.
        n_threads:   0 picks ``min(8, cpu_count())``.
        block_size:  0 picks an adaptive size based on len(data).
    """
    return omni_compress_v8_turbo(
        data, n_threads=n_threads, block_size=block_size, zstd_level=level)


decompress = omni_decompress_v8

__all__ = [
    "compress",
    "decompress",
    "omni_compress_v8_turbo",
    "omni_decompress_v8",
    "NICHE_NAMES",
    "MAGIC_V8",
]

__version__ = "0.9.0"
