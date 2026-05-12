/*
 * pipeline.c
 *
 * Full pipeline in C: detect + compress in a single call.
 * The path from input bytes to compressed output never returns to Python.
 *
 * For each block:
 *   1. Detect the niche (detector embedded below).
 *   2. Apply the matching codec.
 *   3. Return the payload + chosen niche id.
 *
 * Threading: each block is processed in parallel via pthreads.
 *
 * Build:
 *   cc -O3 -march=native -ffast-math -fPIC -shared -pthread \
 *      pipeline.c -lzstd -lm -o libomnicomp_pipeline.so
 */

#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <pthread.h>
#include <sys/utsname.h>
#ifndef _WIN32
#include <unistd.h>
#endif
#include <zstd.h>

#if defined(__aarch64__) || defined(__arm64__)
#include <arm_neon.h>
#endif

/* One ZSTD_DCtx per thread for decode: ZSTD_decompress() allocates/frees
 * scratch state internally; ZSTD_decompressDCtx() reuses the same workspace
 * across many small frames (every generic block + inner zstd frames). */
static size_t zstd_tls_decompress(void *dst, size_t dst_cap,
                                 const void *src, size_t src_size) {
#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
    _Thread_local static ZSTD_DCtx *tls_dctx;
#elif defined(__clang__) || defined(__GNUC__)
    static __thread ZSTD_DCtx *tls_dctx;
#else
    static ZSTD_DCtx *tls_dctx;
#endif
    if (!tls_dctx) {
        tls_dctx = ZSTD_createDCtx();
        if (!tls_dctx)
            return ZSTD_decompress(dst, dst_cap, src, src_size);
    }
    return ZSTD_decompressDCtx(tls_dctx, dst, dst_cap, src, src_size);
}

/* True only when this binary is AArch64 *and* uname says we run on arm64
 * hardware (never enables NEON SIMD without that check). */
static int omnicomp_runtime_aarch64(void) {
#if defined(_WIN32)
    return 0;
#elif defined(__aarch64__) || defined(__arm64__)
    struct utsname u;
    if (uname(&u) != 0) return 0;
    return (strcmp(u.machine, "aarch64") == 0 || strcmp(u.machine, "arm64") == 0);
#else
    return 0;
#endif
}

/* Niche IDs (kept in sync with the Python wrapper). */
#define NICHE_GENERIC               0
#define NICHE_RANDOM                1
#define NICHE_CONSTANT              2
#define NICHE_NUMERIC_F64           3
#define NICHE_NUMERIC_F32           4
#define NICHE_NUMERIC_I64           5
#define NICHE_NUMERIC_I32           6
#define NICHE_NUMERIC_I16           7
#define NICHE_SHUFFLE_4             8
#define NICHE_SHUFFLE_8             9
#define NICHE_SHUFFLE_16           10
#define NICHE_SHUFFLE_20           11
#define NICHE_SHUFFLE_2            14  /* New: for raw int16. */
#define NICHE_TEXT                 12
#define NICHE_STRUCTURED           13
#define NICHE_NUMERIC_F64_PRED_SHUFFLE 19
#define NICHE_NUMERIC_F32_PRED_SHUFFLE 21

typedef union { double f; uint64_t u; } f64_u;
typedef union { float f; uint32_t u; } f32_u;

/* ============ EMBEDDED DETECTOR ============ */
static int log2_init = 0;
static double log2_table[65537];

static void init_log2(void) {
    if (log2_init) return;
    log2_table[0] = 0;
    for (int i = 1; i <= 65536; i++) log2_table[i] = log2((double)i);
    log2_init = 1;
}

typedef struct {
    double H;
    double asciiness;
    double most_common_freq;
    double autocorr_2, autocorr_4, autocorr_8, autocorr_16, autocorr_20;
    double has_brackets;
    int n;
    /* Non-zero if the sample contains any NaN/Inf bit-pattern when
     * interpreted as f64 or f32 at the obvious alignment. Real numeric
     * data is finite; a non-zero value here strongly suggests we are
     * looking at binary content (executable, image, database). */
    int has_nonfinite_f64;
    int has_nonfinite_f32;
} BStats;

static void analyze_inline(const uint8_t *data, size_t n, BStats *out) {
    out->n = (int)n;
    if (n == 0) {
        memset(out, 0, sizeof(BStats));
        return;
    }

    /* For large inputs, sub-sample to keep the detector roughly constant-time. */
    const uint8_t *sample;
    size_t sample_n;
    uint8_t sample_buf[16384];
    if (n <= 16384) {
        sample = data;
        sample_n = n;
    } else {
        size_t chunk = 4096;
        size_t stride = (n - chunk) / 3;
        for (int i = 0; i < 4; i++) {
            size_t off = (size_t)i * stride;
            if (off + chunk > n) off = n - chunk;
            memcpy(sample_buf + i * chunk, data + off, chunk);
        }
        sample = sample_buf;
        sample_n = 16384;
    }

    /* Histogram + ASCII + structural-bracket density. */
    uint32_t hist[256] = {0};
    uint32_t ascii_count = 0;
    uint32_t bracket_count = 0;
    for (size_t i = 0; i < sample_n; i++) {
        uint8_t b = sample[i];
        hist[b]++;
        if (b >= 32 && b < 127) ascii_count++;
        if (b=='{'||b=='}'||b=='['||b==']'||b=='<'||b=='>'||b=='"'||b==','||b==':') bracket_count++;
    }

    /* NaN/Inf bit-pattern scan at f64/f32 alignment. Real numeric data
     * is finite. We only need to find one to disqualify the numeric
     * pred_shuffle niches. */
    out->has_nonfinite_f64 = 0;
    out->has_nonfinite_f32 = 0;
    if (sample_n >= 8) {
        const uint64_t *u = (const uint64_t *)sample;
        size_t m = sample_n / 8;
        for (size_t i = 0; i < m; i++) {
            if (((u[i] >> 52) & 0x7FFu) == 0x7FFu) { out->has_nonfinite_f64 = 1; break; }
        }
    }
    if (sample_n >= 4) {
        const uint32_t *u = (const uint32_t *)sample;
        size_t m = sample_n / 4;
        for (size_t i = 0; i < m; i++) {
            if (((u[i] >> 23) & 0xFFu) == 0xFFu) { out->has_nonfinite_f32 = 1; break; }
        }
    }

    double inv_n = 1.0 / (double)sample_n;
    double log2_n = log2((double)sample_n);
    double H = 0.0;
    uint32_t max_count = 0;
    for (int i = 0; i < 256; i++) {
        if (hist[i] > 0) {
            double p = (double)hist[i] * inv_n;
            uint32_t c = hist[i];
            double log2_c = (c <= 65536) ? log2_table[c] : log2((double)c);
            H -= p * (log2_c - log2_n);
            if (c > max_count) max_count = c;
        }
    }
    out->H = H;
    out->asciiness = (double)ascii_count * inv_n;
    out->most_common_freq = (double)max_count * inv_n;
    out->has_brackets = (double)bracket_count * inv_n;

    /* Early exit: when the block is overwhelmingly ASCII the niche is
     * guaranteed to be TEXT or STRUCTURED (decided by bracket density)
     * and the autocorrelation columns will not change the verdict.
     * Skip the 5-pass AC scan to save cycles. */
    if (out->asciiness > 0.92 && max_count < (uint32_t)(sample_n * 0.6)) {
        out->autocorr_2 = out->autocorr_4 = out->autocorr_8 = 0.0;
        out->autocorr_16 = out->autocorr_20 = 0.0;
        return;
    }

    /* Byte autocorrelations at strides 2, 4, 8, 16, 20. */
    static const int ks[] = {2, 4, 8, 16, 20};
    double *outs[] = {&out->autocorr_2, &out->autocorr_4,
                      &out->autocorr_8, &out->autocorr_16, &out->autocorr_20};
    for (int j = 0; j < 5; j++) {
        int k = ks[j];
        if ((size_t)k >= sample_n) { *outs[j] = 0.0; continue; }
        size_t pairs = sample_n - k;
        size_t matches = 0;
        const uint8_t *a = sample, *b = sample + k;
        for (size_t i = 0; i < pairs; i++) {
            if (a[i] == b[i]) matches++;
        }
        double m = (double)matches / (double)pairs;
        double normalized = (m - 1.0/256.0) * (256.0/255.0);
        if (normalized < 0) normalized = 0;
        *outs[j] = normalized;
    }
}

static int detect_niche_inline(BStats *s, size_t n) {
    if (n < 64) return NICHE_GENERIC;
    if (s->most_common_freq > 0.95) return NICHE_CONSTANT;

    double max_ac = s->autocorr_2;
    if (s->autocorr_4 > max_ac) max_ac = s->autocorr_4;
    if (s->autocorr_8 > max_ac) max_ac = s->autocorr_8;
    if (s->autocorr_16 > max_ac) max_ac = s->autocorr_16;
    /* Strong cyclic pattern with low entropy: zstd will crush it faster. */
    if (max_ac > 0.9 && s->H < 5.5) return NICHE_GENERIC;

    if (s->H > 7.7 && s->autocorr_2 < 0.02 && s->autocorr_4 < 0.02 &&
            s->autocorr_8 < 0.02 && s->autocorr_16 < 0.02) return NICHE_RANDOM;

    /* Integer arrays with constant high-order bytes.
     * The smallest k with AC(k) within 80% of the max is the actual element
     * size. We additionally require that the dominant AC is much higher than
     * the baseline so that uniformly-correlated binary content (e.g. broad
     * AC ~ 0.4 across all strides) doesn't get misrouted to a shuffle codec. */
    int has_2 = s->autocorr_2 > 0.4;
    int has_4 = s->autocorr_4 > 0.4;
    int has_8 = s->autocorr_8 > 0.4;
    if (has_2 || has_4 || has_8) {
        double max_v = 0;
        if (has_2) max_v = s->autocorr_2;
        if (has_4 && s->autocorr_4 > max_v) max_v = s->autocorr_4;
        if (has_8 && s->autocorr_8 > max_v) max_v = s->autocorr_8;
        /* Skip this branch when AC is broadly elevated without a clear peak. */
        double broad_floor = 0.30;
        int broadly_correlated =
            (s->autocorr_2 > broad_floor && s->autocorr_4 > broad_floor &&
             s->autocorr_8 > broad_floor && s->autocorr_16 > broad_floor &&
             max_v < 0.65);
        if (!broadly_correlated) {
            if (has_2 && s->autocorr_2 >= max_v * 0.8) return NICHE_NUMERIC_I16;
            if (has_4 && s->autocorr_4 >= max_v * 0.8) return NICHE_NUMERIC_I32;
            if (has_8 && s->autocorr_8 >= max_v * 0.8) return NICHE_NUMERIC_I64;
        }
    }

    /* Floating-point / strided structure with moderate autocorrelation.
     *
     * The pred_shuffle codec only helps on genuine numeric data, and
     * byte-shuffle only helps on truly k-byte-structured data. The
     * empirical signature of "real" k-byte structure is that AC(k) is
     * a LOCAL MAXIMUM, not just some elevated value: a 64-bit array
     * has AC8 noticeably greater than AC4 and AC2, while a binary
     * blob can have AC8 ~ 0.10 with AC2 / AC4 also ~ 0.10.
     *
     * We therefore require both:
     *   (a) AC at the niche's stride exceeds a base threshold, and
     *   (b) AC at the stride exceeds AC at every smaller stride that
     *       would also be a candidate.
     *
     * Plus the numeric pred_shuffle niches require finite f64 / f32
     * bit-patterns (NaN/Inf present means we are looking at binary). */
    if (s->H > 6.0) {
        double a2 = s->autocorr_2, a4 = s->autocorr_4;
        double a8 = s->autocorr_8, a16 = s->autocorr_16, a20 = s->autocorr_20;
        struct {
            double v; int niche; double thr;
            int local_max_over_2, local_max_over_4, local_max_over_8;
            int needs_finite_f32, needs_finite_f64;
        } cands[5] = {
            {a2,  NICHE_NUMERIC_I16, 0.15, 0, 0, 0, 0, 0},
            {a4,  NICHE_NUMERIC_F32, 0.15, 1, 0, 0, 1, 0},
            {a8,  NICHE_NUMERIC_F64, 0.15, 1, 1, 0, 0, 1},
            {a16, NICHE_SHUFFLE_16,  0.10, 1, 1, 1, 0, 0},
            {a20, NICHE_SHUFFLE_20,  0.10, 1, 1, 1, 0, 0},
        };
        int best = -1;
        double best_score = 0.0;
        for (int i = 0; i < 5; i++) {
            if (cands[i].v < cands[i].thr) continue;
            if (cands[i].local_max_over_2 && cands[i].v <= a2 * 1.10) continue;
            if (cands[i].local_max_over_4 && cands[i].v <= a4 * 1.10) continue;
            if (cands[i].local_max_over_8 && cands[i].v <= a8 * 1.10) continue;
            if (cands[i].needs_finite_f32 && s->has_nonfinite_f32) continue;
            if (cands[i].needs_finite_f64 && s->has_nonfinite_f64) continue;
            if (cands[i].v > best_score) { best_score = cands[i].v; best = i; }
        }
        if (best >= 0) return cands[best].niche;
    }

    /* int16 audio. */
    if (n % 2 == 0 && s->H > 7.0 && s->asciiness < 0.5
            && s->autocorr_2 > 0.005 && s->autocorr_2 < 0.1) return NICHE_NUMERIC_I16;

    /* Low-cardinality floats: keep the original heuristic but require finite
     * bit patterns (real ratings/quantised data is finite). */
    if (n % 8 == 0 && s->H > 3.0 && s->H < 6.0 && s->asciiness < 0.7
            && s->autocorr_8 > 0.15 && max_ac < 0.85
            && !s->has_nonfinite_f64) return NICHE_NUMERIC_F64;

    if (s->asciiness > 0.85 && s->has_brackets > 0.05) return NICHE_STRUCTURED;
    if (s->asciiness > 0.85) return NICHE_TEXT;
    return NICHE_GENERIC;
}

/* ============ map detected niche -> codec to apply ============ */
static int map_niche_to_codec(int detected) {
    switch (detected) {
        case NICHE_NUMERIC_F64: return NICHE_NUMERIC_F64_PRED_SHUFFLE;
        case NICHE_NUMERIC_F32: return NICHE_NUMERIC_F32_PRED_SHUFFLE;
        case NICHE_NUMERIC_I64: return NICHE_SHUFFLE_8;
        case NICHE_NUMERIC_I32: return NICHE_SHUFFLE_4;
        /* For int16, shuffle_8 captures stereo-PCM patterns better than shuffle_4. */
        case NICHE_NUMERIC_I16: return NICHE_SHUFFLE_8;
        default: return detected;
    }
}

/* True if the codec can be applied to a buffer of size n.
 * The codecs themselves also handle this via their flavour byte, but the
 * dispatcher prefers to pick a different codec when it can. */
static int codec_applicable(int codec_id, size_t n) {
    switch (codec_id) {
        case NICHE_SHUFFLE_4:  return (n % 4 == 0);
        case NICHE_SHUFFLE_8:  return (n % 8 == 0);
        case NICHE_SHUFFLE_16: return (n % 16 == 0);
        case NICHE_SHUFFLE_20: return (n % 20 == 0);
        case NICHE_NUMERIC_F64_PRED_SHUFFLE: return (n >= 24 && n % 8 == 0);
        case NICHE_NUMERIC_F32_PRED_SHUFFLE: return (n >= 12 && n % 4 == 0);
        default: return 1;
    }
}

/* ============ CODECS (compact) ============ */
static size_t comp_zstd(const uint8_t *src, size_t n, uint8_t *dst, size_t dst_cap, int level) {
    return ZSTD_compress(dst, dst_cap, src, n, level);
}
static size_t decomp_zstd(const uint8_t *src, size_t n, uint8_t *dst, size_t dst_cap) {
    return zstd_tls_decompress(dst, dst_cap, src, n);
}

/* Self-describing codec output:
 *   first byte = 0x01 -> specialised path follows
 *   first byte = 0x00 -> plain-zstd fallback follows
 * Decoder reads the tag, dispatches accordingly. This guarantees correct
 * round-trip even if the dispatcher chooses a codec whose preconditions
 * (e.g. n % k == 0) are not met. */

static size_t comp_shuffle(const uint8_t *src, size_t n, uint8_t *dst, size_t dst_cap,
                            int level, int k) {
    if (dst_cap < 1) return 0;
    if (n % k != 0 || n == 0) {
        dst[0] = 0x00;
        size_t out = ZSTD_compress(dst + 1, dst_cap - 1, src, n, level);
        if (ZSTD_isError(out)) return 0;
        return out + 1;
    }
    size_t n_elems = n / k;
    uint8_t *tmp = (uint8_t *)malloc(n);
    if (!tmp) return 0;
    /* Cache-friendly transpose: process the input in tiles of TILE rows,
     * writing one output stream at a time. The original layout did
     * scattered writes across k output streams per i, which cost a lot
     * of cache misses for k * n_elems destination bytes. */
    enum { TILE = 256 };
    for (size_t ii = 0; ii < n_elems; ii += TILE) {
        size_t end = (ii + TILE > n_elems) ? n_elems : ii + TILE;
        for (int b = 0; b < k; b++) {
            uint8_t *dst_b = tmp + b * n_elems;
            for (size_t i = ii; i < end; i++) {
                dst_b[i] = src[i * k + b];
            }
        }
    }
    dst[0] = 0x01;
    size_t out = ZSTD_compress(dst + 1, dst_cap - 1, tmp, n, level);
    free(tmp);
    if (ZSTD_isError(out)) return 0;
    return out + 1;
}

#if defined(__aarch64__) || defined(__arm64__)
/* NEON inverse of byte-shuffle for k==4: four planes (length n_elems each) ->
 * interleaved rows dst[i*4+b]. vst4_u8 writes 8 rows (32 bytes) per step; tail
 * is scalar. Only used when omnicomp_runtime_aarch64() is true (uname gate). */
static void shuffle_inv_deinterleave_neon_k4(const uint8_t *tmp, size_t n_elems,
                                             uint8_t *dst) {
    const uint8_t *p0 = tmp;
    const uint8_t *p1 = tmp + n_elems;
    const uint8_t *p2 = tmp + 2u * n_elems;
    const uint8_t *p3 = tmp + 3u * n_elems;
    size_t ii = 0;
    for (; ii + 8 <= n_elems; ii += 8) {
        uint8x8x4_t pack;
        pack.val[0] = vld1_u8(p0 + ii);
        pack.val[1] = vld1_u8(p1 + ii);
        pack.val[2] = vld1_u8(p2 + ii);
        pack.val[3] = vld1_u8(p3 + ii);
        vst4_u8(dst + ii * 4u, pack);
    }
    for (; ii < n_elems; ii++) {
        dst[ii * 4u + 0] = p0[ii];
        dst[ii * 4u + 1] = p1[ii];
        dst[ii * 4u + 2] = p2[ii];
        dst[ii * 4u + 3] = p3[ii];
    }
}
#endif

static size_t decomp_shuffle(const uint8_t *src, size_t n, uint8_t *dst,
                              size_t dst_cap, int k, size_t orig_size) {
    if (n < 1) return 0;
    if (src[0] == 0x00) {
        return zstd_tls_decompress(dst, dst_cap, src + 1, n - 1);
    }
    /* src[0] == 0x01: shuffled payload. The ZSTD frame expands to exactly the
     * original block size (k plane layout). Never allocate dst_cap bytes here:
     * callers pass remaining output window (often huge); malloc(dst_cap) was
     * O(file) per block and destroyed decompression throughput. */
    if (orig_size == 0 || orig_size > dst_cap) return 0;
    uint8_t *tmp = (uint8_t *)malloc(orig_size);
    if (!tmp) return 0;
    size_t decomp_size = zstd_tls_decompress(tmp, orig_size, src + 1, n - 1);
    if (ZSTD_isError(decomp_size)) { free(tmp); return 0; }
    if (decomp_size != orig_size || (decomp_size % (size_t)k) != 0) {
        free(tmp);
        return 0;
    }
    size_t n_elems = decomp_size / k;
#if defined(__aarch64__) || defined(__arm64__)
    if (omnicomp_runtime_aarch64() && k == 4 && n_elems >= 8) {
        shuffle_inv_deinterleave_neon_k4(tmp, n_elems, dst);
        free(tmp);
        return decomp_size;
    }
#endif
    /* Cache-friendly inverse transpose (scalar; all k including NEON tail cases). */
    enum { TILE = 256 };
    for (size_t ii = 0; ii < n_elems; ii += TILE) {
        size_t end = (ii + TILE > n_elems) ? n_elems : ii + TILE;
        for (int b = 0; b < k; b++) {
            const uint8_t *src_b = tmp + b * n_elems;
            for (size_t i = ii; i < end; i++) {
                dst[i * k + b] = src_b[i];
            }
        }
    }
    free(tmp);
    return decomp_size;
}

/* NaN/Inf preflight: returns 1 if the buffer (read as f64) contains any NaN
 * or Inf bit-pattern. The pred_shuffle predictor uses FP arithmetic on these
 * values which can produce non-deterministic NaN payloads under aggressive
 * optimisation, breaking the round-trip. We refuse to apply pred_shuffle on
 * such buffers and let the dispatcher fall back to plain zstd. */
static int has_nonfinite_f64(const uint8_t *src, size_t n) {
    if (n < 8) return 0;
    const uint64_t *u = (const uint64_t *)src;
    size_t m = n / 8;
    for (size_t i = 0; i < m; i++) {
        /* exponent all-ones: NaN or Inf */
        if (((u[i] >> 52) & 0x7FFu) == 0x7FFu) return 1;
    }
    return 0;
}

static int has_nonfinite_f32(const uint8_t *src, size_t n) {
    if (n < 4) return 0;
    const uint32_t *u = (const uint32_t *)src;
    size_t m = n / 4;
    for (size_t i = 0; i < m; i++) {
        if (((u[i] >> 23) & 0xFFu) == 0xFFu) return 1;
    }
    return 0;
}

static size_t comp_f64_pred_shuffle(const uint8_t *src, size_t n, uint8_t *dst, size_t dst_cap, int level) {
    if (dst_cap < 1) return 0;
    if (n < 24 || n % 8 != 0 || has_nonfinite_f64(src, n)) {
        dst[0] = 0x00;
        size_t out = ZSTD_compress(dst + 1, dst_cap - 1, src, n, level);
        if (ZSTD_isError(out)) return 0;
        return out + 1;
    }
    size_t n_doubles = n / 8;
    size_t n_res = n_doubles - 2;
    size_t mid_size = 16 + 8 * n_res;
    uint8_t *tmp = (uint8_t *)malloc(mid_size);
    if (!tmp) return 0;
    memcpy(tmp, src, 16);
    uint8_t *res_buf = tmp + 16;
    const double *x = (const double *)src;
    for (size_t i = 0; i < n_res; i++) {
        f64_u xb, pb;
        xb.f = x[i + 2];
        pb.f = 2.0 * x[i + 1] - x[i];
        uint64_t res = xb.u ^ pb.u;
        for (int b = 0; b < 8; b++) res_buf[b * n_res + i] = (uint8_t)(res >> (b * 8));
    }
    dst[0] = 0x01;
    size_t out = ZSTD_compress(dst + 1, dst_cap - 1, tmp, mid_size, level);
    free(tmp);
    if (ZSTD_isError(out)) return 0;
    return out + 1;
}

static size_t decomp_f64_pred_shuffle(const uint8_t *src, size_t n, uint8_t *dst,
                                       size_t dst_cap, size_t orig_size) {
    if (n < 1) return 0;
    if (src[0] == 0x00) {
        return zstd_tls_decompress(dst, dst_cap, src + 1, n - 1);
    }
    /* Middle buffer is exactly orig_size bytes for a valid pred_shuffle block. */
    if (orig_size < 24 || orig_size > dst_cap || orig_size % 8 != 0) return 0;
    uint8_t *tmp = (uint8_t *)malloc(orig_size);
    if (!tmp) return 0;
    size_t mid_size = zstd_tls_decompress(tmp, orig_size, src + 1, n - 1);
    if (ZSTD_isError(mid_size)) { free(tmp); return 0; }
    if (mid_size != orig_size) { free(tmp); return 0; }
    memcpy(dst, tmp, 16);
    const uint8_t *res_buf = tmp + 16;
    size_t n_res = (mid_size - 16) / 8;
    double *x = (double *)dst;
    for (size_t i = 0; i < n_res; i++) {
        uint64_t res = 0;
        for (int b = 0; b < 8; b++) res |= ((uint64_t)res_buf[b * n_res + i]) << (b * 8);
        f64_u pb, xb;
        pb.f = 2.0 * x[i + 1] - x[i];
        xb.u = res ^ pb.u;
        x[i + 2] = xb.f;
    }
    free(tmp);
    return 16 + 8 * n_res;
}

static size_t comp_f32_pred_shuffle(const uint8_t *src, size_t n, uint8_t *dst, size_t dst_cap, int level) {
    if (dst_cap < 1) return 0;
    if (n < 12 || n % 4 != 0 || has_nonfinite_f32(src, n)) {
        dst[0] = 0x00;
        size_t out = ZSTD_compress(dst + 1, dst_cap - 1, src, n, level);
        if (ZSTD_isError(out)) return 0;
        return out + 1;
    }
    size_t n_floats = n / 4;
    size_t n_res = n_floats - 2;
    size_t mid_size = 8 + 4 * n_res;
    uint8_t *tmp = (uint8_t *)malloc(mid_size);
    if (!tmp) return 0;
    memcpy(tmp, src, 8);
    uint8_t *res_buf = tmp + 8;
    const float *x = (const float *)src;
    for (size_t i = 0; i < n_res; i++) {
        f32_u xb, pb;
        xb.f = x[i + 2];
        pb.f = 2.0f * x[i + 1] - x[i];
        uint32_t res = xb.u ^ pb.u;
        for (int b = 0; b < 4; b++) res_buf[b * n_res + i] = (uint8_t)(res >> (b * 8));
    }
    dst[0] = 0x01;
    size_t out = ZSTD_compress(dst + 1, dst_cap - 1, tmp, mid_size, level);
    free(tmp);
    if (ZSTD_isError(out)) return 0;
    return out + 1;
}

static size_t decomp_f32_pred_shuffle(const uint8_t *src, size_t n, uint8_t *dst,
                                       size_t dst_cap, size_t orig_size) {
    if (n < 1) return 0;
    if (src[0] == 0x00) {
        return zstd_tls_decompress(dst, dst_cap, src + 1, n - 1);
    }
    if (orig_size < 12 || orig_size > dst_cap || orig_size % 4 != 0) return 0;
    uint8_t *tmp = (uint8_t *)malloc(orig_size);
    if (!tmp) return 0;
    size_t mid_size = zstd_tls_decompress(tmp, orig_size, src + 1, n - 1);
    if (ZSTD_isError(mid_size)) { free(tmp); return 0; }
    if (mid_size != orig_size) { free(tmp); return 0; }
    memcpy(dst, tmp, 8);
    const uint8_t *res_buf = tmp + 8;
    size_t n_res = (mid_size - 8) / 4;
    float *x = (float *)dst;
    for (size_t i = 0; i < n_res; i++) {
        uint32_t res = 0;
        for (int b = 0; b < 4; b++) res |= ((uint32_t)res_buf[b * n_res + i]) << (b * 8);
        f32_u pb, xb;
        pb.f = 2.0f * x[i + 1] - x[i];
        xb.u = res ^ pb.u;
        x[i + 2] = xb.f;
    }
    free(tmp);
    return 8 + 4 * n_res;
}

static size_t comp_rle(const uint8_t *src, size_t n, uint8_t *dst, size_t dst_cap, int level) {
    if (n == 0) return 0;
    int all_same = 1;
    for (size_t i = 1; i < n; i++) if (src[i] != src[0]) { all_same = 0; break; }
    if (all_same) {
        if (dst_cap < 2) return 0;
        dst[0] = 0x01; dst[1] = src[0];
        return 2;
    }
    if (dst_cap < 1) return 0;
    dst[0] = 0x00;
    size_t out = ZSTD_compress(dst + 1, dst_cap - 1, src, n, level);
    if (ZSTD_isError(out)) return 0;
    return out + 1;
}

static size_t decomp_rle(const uint8_t *src, size_t n, uint8_t *dst, size_t dst_cap, size_t orig_size) {
    if (n == 0) return 0;
    if (src[0] == 0x01) {
        if (orig_size > dst_cap) return 0;
        memset(dst, src[1], orig_size);
        return orig_size;
    }
    return zstd_tls_decompress(dst, dst_cap, src + 1, n - 1);
}

/* ============ FAST DISPATCH ============ */
static size_t compress_with_codec(const uint8_t *src, size_t n, int codec_id,
                                    uint8_t *dst, size_t dst_cap, int level) {
    switch (codec_id) {
        case NICHE_RANDOM:
            if (n > dst_cap) return 0;
            memcpy(dst, src, n);
            return n;
        case NICHE_CONSTANT:
            return comp_rle(src, n, dst, dst_cap, level);
        case NICHE_SHUFFLE_4:  return comp_shuffle(src, n, dst, dst_cap, level, 4);
        case NICHE_SHUFFLE_8:  return comp_shuffle(src, n, dst, dst_cap, level, 8);
        case NICHE_SHUFFLE_16: return comp_shuffle(src, n, dst, dst_cap, level, 16);
        case NICHE_SHUFFLE_20: return comp_shuffle(src, n, dst, dst_cap, level, 20);
        case NICHE_NUMERIC_F64_PRED_SHUFFLE: return comp_f64_pred_shuffle(src, n, dst, dst_cap, level);
        case NICHE_NUMERIC_F32_PRED_SHUFFLE: return comp_f32_pred_shuffle(src, n, dst, dst_cap, level);
        default: return comp_zstd(src, n, dst, dst_cap, level);
    }
}

size_t decompress_with_codec(const uint8_t *src, size_t n, int codec_id,
                              uint8_t *dst, size_t dst_cap, size_t orig_size) {
    switch (codec_id) {
        case NICHE_RANDOM:
            if (n > dst_cap) return 0;
            memcpy(dst, src, n);
            return n;
        case NICHE_CONSTANT: return decomp_rle(src, n, dst, dst_cap, orig_size);
        case NICHE_SHUFFLE_4:  return decomp_shuffle(src, n, dst, dst_cap, 4, orig_size);
        case NICHE_SHUFFLE_8:  return decomp_shuffle(src, n, dst, dst_cap, 8, orig_size);
        case NICHE_SHUFFLE_16: return decomp_shuffle(src, n, dst, dst_cap, 16, orig_size);
        case NICHE_SHUFFLE_20: return decomp_shuffle(src, n, dst, dst_cap, 20, orig_size);
        case NICHE_NUMERIC_F64_PRED_SHUFFLE:
            return decomp_f64_pred_shuffle(src, n, dst, dst_cap, orig_size);
        case NICHE_NUMERIC_F32_PRED_SHUFFLE:
            return decomp_f32_pred_shuffle(src, n, dst, dst_cap, orig_size);
        default: return decomp_zstd(src, n, dst, dst_cap);
    }
}

static uint32_t read_u32_be(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

#ifndef _WIN32
static int omn8_decomp_thread_budget(void) {
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    if (n < 1) n = 1;
    if (n > 16) n = 16;
    return (int)n;
}
#else
static int omn8_decomp_thread_budget(void) { return 4; }
#endif

typedef struct {
    const uint8_t *payload;
    size_t csize;
    size_t orig;
    int nid;
    size_t dst_off;
} Omn8BlockRec;

typedef struct {
    Omn8BlockRec *rec;
    uint8_t *dst_base;
    volatile int *fail;
} Omn8DecompThr;

static void *omn8_decomp_worker(void *arg) {
    Omn8DecompThr *t = (Omn8DecompThr *)arg;
    Omn8BlockRec *b = t->rec;
    size_t w = decompress_with_codec(
        b->payload, b->csize, b->nid,
        t->dst_base + b->dst_off, b->orig, b->orig);
    if (w != b->orig)
        *t->fail = 1;
    return NULL;
}

/* Full v8 container decode in one call (matches Python ``omni_decompress_v8``
 * layout). Parallelises independent blocks across pthreads when there are
 * enough blocks (Mozilla-scale inputs often split into 6–8 × 8 MiB blocks).
 * Each thread gets its own ZSTD_DCtx via thread-local storage. */
int pipeline_decompress_omn8(const uint8_t *comp, size_t comp_len,
                              uint8_t *dst, size_t dst_cap) {
    if (comp_len < 12) return -1;
    if (comp[0] != (uint8_t)'O' || comp[1] != (uint8_t)'M' ||
        comp[2] != (uint8_t)'N' || comp[3] != (uint8_t)'8')
        return -2;
    size_t total = ((size_t)comp[4] << 24) | ((size_t)comp[5] << 16) |
                   ((size_t)comp[6] << 8) | (size_t)comp[7];
    uint32_t n_blocks = read_u32_be(comp + 8);
    if (total > dst_cap) return -4;
    if (total == 0) {
        if (n_blocks != 0) return -11;
        if (comp_len != 12) return -9;
        return 0;
    }

    Omn8BlockRec *blocks =
        (Omn8BlockRec *)malloc((size_t)n_blocks * sizeof(Omn8BlockRec));
    if (!blocks) return -20;

    size_t pos = 12;
    size_t out_off = 0;
    for (uint32_t bi = 0; bi < n_blocks; bi++) {
        if (pos + 9 > comp_len) {
            free(blocks);
            return -5;
        }
        int nid = (int)comp[pos++];
        uint32_t orig = read_u32_be(comp + pos);
        pos += 4;
        uint32_t csize = read_u32_be(comp + pos);
        pos += 4;
        if ((size_t)csize > comp_len - pos) {
            free(blocks);
            return -6;
        }
        if (out_off + (size_t)orig > dst_cap) {
            free(blocks);
            return -7;
        }
        blocks[bi].payload = comp + pos;
        blocks[bi].csize = (size_t)csize;
        blocks[bi].orig = (size_t)orig;
        blocks[bi].nid = nid;
        blocks[bi].dst_off = out_off;
        pos += (size_t)csize;
        out_off += (size_t)orig;
    }
    if (pos != comp_len) {
        free(blocks);
        return -9;
    }
    if (out_off != total) {
        free(blocks);
        return -10;
    }

    volatile int fail = 0;
    enum { PARALLEL_MIN_BLOCKS = 3 };
    int maxt = omn8_decomp_thread_budget();
    /* Benchmark can set OMNICOMP_OMN8_DECOMP_SINGLE_THREAD=1 so decode stays
     * single-threaded inside one container while Python parallelises across
     * independent streams (fair vs parallel zstd frames). */
    const char *st = getenv("OMNICOMP_OMN8_DECOMP_SINGLE_THREAD");
    int allow_parallel = !(st && st[0] == '1' && st[1] == '\0');
    if (allow_parallel && (int)n_blocks >= PARALLEL_MIN_BLOCKS && maxt > 1) {
        uint32_t bi = 0;
        while (bi < n_blocks && !fail) {
            int batch = (int)(n_blocks - bi);
            if (batch > maxt) batch = maxt;
            pthread_t ths[16];
            Omn8DecompThr args[16];
            if (batch > 16) batch = 16;
            for (int t = 0; t < batch; t++) {
                args[t].rec = &blocks[(size_t)bi + (size_t)t];
                args[t].dst_base = dst;
                args[t].fail = &fail;
                if (pthread_create(&ths[t], NULL, omn8_decomp_worker, &args[t]) != 0) {
                    for (int k = 0; k < t; k++) pthread_join(ths[k], NULL);
                    free(blocks);
                    return -21;
                }
            }
            for (int t = 0; t < batch; t++) pthread_join(ths[t], NULL);
            bi += (uint32_t)batch;
        }
        free(blocks);
        return fail ? -8 : 0;
    }

    for (uint32_t bi = 0; bi < n_blocks; bi++) {
        Omn8BlockRec *b = &blocks[bi];
        size_t w = decompress_with_codec(
            b->payload, b->csize, b->nid,
            dst + b->dst_off, dst_cap - b->dst_off, b->orig);
        if (w != b->orig) {
            free(blocks);
            return -8;
        }
    }
    free(blocks);
    return 0;
}

/* ============ FULL PER-BLOCK PIPELINE: detect+compress ============ */
typedef struct {
    const uint8_t *block_src;
    size_t block_len;
    int zstd_level;
    /* Output. */
    uint8_t *block_dst;
    size_t block_dst_cap;
    size_t block_dst_written;
    int chosen_niche;
    /* Used when RANDOM is detected: still try plain zstd as a fallback. */
    int verify_random;
} PipelineJob;

static void *worker_pipeline(void *arg) {
    PipelineJob *job = (PipelineJob *)arg;

    /* 1. Detect. */
    BStats stats;
    init_log2();
    analyze_inline(job->block_src, job->block_len, &stats);
    int detected = detect_niche_inline(&stats, job->block_len);

    /* 2. Map to a suitable codec. If the codec cannot be applied to the
     * current block size, fall back to plain zstd (NICHE_GENERIC). */
    int codec = map_niche_to_codec(detected);
    if (!codec_applicable(codec, job->block_len)) codec = NICHE_GENERIC;

    /* 3. Map user level -> per-block strategy.
     *
     * The user passes a single integer "level" which we map onto three
     * actual zstd levels and a cascade policy. This gives a single knob
     * that the user can dial from "fast" to "strong":
     *
     *   user level | comp_level | codec_internal | cascade_level | cascade
     *  ------------+------------+----------------+---------------+-----------
     *      1       |     1      |       1        |       1       | smart
     *      3 (def) |     3      |       1        |       3       | smart
     *      6       |     6      |       2        |       6       | always
     *     >=9      |   user     |       3        |     user      | always
     *
     * "comp_level" drives plain zstd (text / structured / generic).
     * "codec_internal" drives the trailing zstd inside a specialised
     * codec — which is fed already-decorrelated bytes, so a low level
     * is usually enough.
     * "cascade_level" is the level used for the safety-net plain-zstd
     * comparison (only when the cascade runs).
     *
     * Level 1 uses the same smart cascade as level 2–3 so misclassified
     * specialised blocks can fall back to zstd-1 (cheap baseline). */
    int user_level = job->zstd_level;
    int comp_level = user_level;
    int codec_internal_level;
    int cascade_level = user_level;
    int cascade_policy;  /* 1=smart, 2=always */
    if (user_level <= 1) {
        /* Fast: still cascade against zstd-1, but avoid codec output
         * regressions (cheap because zstd-1 is ~800 MB/s anyway).
         * The predictor codec is the riskiest one without a cascade,
         * so we keep the cascade on for both shuffle and predictor. */
        codec_internal_level = 1;
        cascade_policy = 1;
    } else if (user_level <= 3) {
        codec_internal_level = 1;
        cascade_policy = 1;
    } else if (user_level <= 6) {
        codec_internal_level = 2;
        cascade_policy = 2;
    } else if (user_level <= 9) {
        codec_internal_level = 3;
        cascade_policy = 2;
    } else if (user_level <= 12) {
        codec_internal_level = 6;
        cascade_policy = 2;
    } else if (user_level <= 15) {
        codec_internal_level = 9;
        cascade_policy = 2;
    } else if (user_level <= 19) {
        codec_internal_level = 12;
        cascade_policy = 2;
    } else {
        /* L20-22: maximum effort. The specialised codec runs at zstd-15
         * internally; cascade at user level. The codec wins on data with
         * exploitable structure (numeric arrays, stride-k records),
         * cascade wins on text/code. Cost: ~2x slower than zstd at the
         * same level, but with the same ratio. */
        codec_internal_level = 15;
        cascade_policy = 2;
    }

    int is_byte_shuffle =
        (codec == NICHE_SHUFFLE_4)  || (codec == NICHE_SHUFFLE_8)  ||
        (codec == NICHE_SHUFFLE_16) || (codec == NICHE_SHUFFLE_20);
    int is_pred_shuffle =
        (codec == NICHE_NUMERIC_F64_PRED_SHUFFLE) ||
        (codec == NICHE_NUMERIC_F32_PRED_SHUFFLE);
    int specialised_codec = is_byte_shuffle || is_pred_shuffle;

    /* For specialised codecs, use codec_internal_level for the trailing
     * zstd. For text / structured / generic, use comp_level directly. */
    int level_for_codec = specialised_codec ? codec_internal_level : comp_level;

    size_t written = compress_with_codec(
        job->block_src, job->block_len, codec,
        job->block_dst, job->block_dst_cap, level_for_codec);

    /* 4. COMPETITIVE CASCADE.
     *
     * Decision matrix (see cascade_policy assignments above; there is no
     * longer a "never cascade" mode—level 1 still uses smart cascade):
     *   - cascade_policy == 1 (smart): run for shuffle, run for predictor
     *     unless it clearly won (< ~0.3 ratio), run for random, run when
     *     codec barely compressed (> ~0.88 ratio)
     *   - cascade_policy == 2 (always): run for every specialised codec
     *     and for random; skip only for text/structured/generic */
    /* Require a stronger win (< ~30% of raw size) before skipping the
     * competitive baseline for predictor codecs under smart cascade. */
    int pred_clearly_won = is_pred_shuffle && (written * 10 < job->block_len * 3);
    int run_baseline = 0;
    /* "Barely compressed" trigger: compare to plain zstd when the specialised
     * output still exceeds ~88% of the raw block (was 90%). Empirically this
     * recovers a few tenths of a percent of ratio on mixed corpora without
     * changing the decompressor—only which payload is stored per block. */
    int specialised_weak = (written * 100 > job->block_len * 88);
    if (cascade_policy == 1) {
        run_baseline =
            (detected == NICHE_RANDOM) ||
            is_byte_shuffle ||
            (is_pred_shuffle && !pred_clearly_won) ||
            specialised_weak;
    } else if (cascade_policy == 2) {
        run_baseline =
            specialised_codec ||
            (detected == NICHE_RANDOM) ||
            specialised_weak;
    }

    if (run_baseline) {
        size_t alt_cap = job->block_dst_cap;
        uint8_t *alt = (uint8_t *)malloc(alt_cap);
        if (alt) {
            size_t alt_written = comp_zstd(job->block_src, job->block_len,
                                            alt, alt_cap, cascade_level);
            if (!ZSTD_isError(alt_written) && alt_written < written) {
                memcpy(job->block_dst, alt, alt_written);
                written = alt_written;
                codec = NICHE_GENERIC;
            }
            free(alt);
        }
    }

    job->block_dst_written = written;
    job->chosen_niche = codec;
    return NULL;
}

int pipeline_compress_blocks(
    const uint8_t **block_srcs,
    const size_t *block_lens,
    uint8_t **block_dsts,
    const size_t *block_dst_caps,
    size_t *block_dst_writtens,
    int *chosen_niches,
    int n_blocks,
    int zstd_level,
    int n_threads
) {
    if (n_threads < 1) n_threads = 1;
    if (n_threads > n_blocks) n_threads = n_blocks;

    PipelineJob *jobs = (PipelineJob *)malloc(n_blocks * sizeof(PipelineJob));
    if (!jobs) return -1;
    pthread_t *threads = (pthread_t *)malloc(n_threads * sizeof(pthread_t));
    if (!threads) { free(jobs); return -1; }

    for (int i = 0; i < n_blocks; i++) {
        jobs[i].block_src = block_srcs[i];
        jobs[i].block_len = block_lens[i];
        jobs[i].zstd_level = zstd_level;
        jobs[i].block_dst = block_dsts[i];
        jobs[i].block_dst_cap = block_dst_caps[i];
        jobs[i].block_dst_written = 0;
        jobs[i].chosen_niche = NICHE_GENERIC;
        jobs[i].verify_random = 1;
    }

    int next_job = 0;
    while (next_job < n_blocks) {
        int batch = n_blocks - next_job;
        if (batch > n_threads) batch = n_threads;
        for (int t = 0; t < batch; t++) {
            pthread_create(&threads[t], NULL, worker_pipeline, &jobs[next_job + t]);
        }
        for (int t = 0; t < batch; t++) {
            pthread_join(threads[t], NULL);
        }
        next_job += batch;
    }

    for (int i = 0; i < n_blocks; i++) {
        block_dst_writtens[i] = jobs[i].block_dst_written;
        chosen_niches[i] = jobs[i].chosen_niche;
    }
    free(jobs);
    free(threads);
    return 0;
}

size_t my_zstd_compress_bound(size_t src_size) {
    return ZSTD_compressBound(src_size);
}
