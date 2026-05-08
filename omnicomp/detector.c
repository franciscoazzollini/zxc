/*
 * detector.c
 *
 * Pure-C niche detector. Standalone version of the detector that lives
 * inlined inside pipeline.c. It computes Shannon entropy + byte-level
 * autocorrelation in a single pass over memory.
 *
 * This file is provided for reference, micro-benchmarking and as a
 * drop-in detector for callers that do not need the full pipeline.
 *
 * Build:
 *   cc -O3 -march=native -ffast-math -fPIC -shared detector.c -o libdetector.so
 *
 * To get peak performance we rely on:
 *   - One pass over the block memory.
 *   - A 256-entry stack histogram.
 *   - Vectorisable arithmetic (GCC/Clang auto-vectorise with -march=native).
 *
 * Niche IDs (kept in sync with pipeline.c and the Python wrapper):
 *   0=GENERIC, 1=RANDOM, 2=CONSTANT
 *   3=NUMERIC_F64, 4=NUMERIC_F32, 5=NUMERIC_I64, 6=NUMERIC_I32, 7=NUMERIC_I16
 *   8=SHUFFLE_4, 9=SHUFFLE_8, 10=SHUFFLE_16, 11=SHUFFLE_20
 *   12=TEXT, 13=STRUCTURED
 */

#include <stdint.h>
#include <stddef.h>
#include <math.h>
#include <string.h>

#define NICHE_GENERIC       0
#define NICHE_RANDOM        1
#define NICHE_CONSTANT      2
#define NICHE_NUMERIC_F64   3
#define NICHE_NUMERIC_F32   4
#define NICHE_NUMERIC_I64   5
#define NICHE_NUMERIC_I32   6
#define NICHE_NUMERIC_I16   7
#define NICHE_SHUFFLE_4     8
#define NICHE_SHUFFLE_8     9
#define NICHE_SHUFFLE_16   10
#define NICHE_SHUFFLE_20   11
#define NICHE_TEXT         12
#define NICHE_STRUCTURED   13

typedef struct {
    double H;
    double asciiness;
    double most_common_freq;
    double autocorr_2;
    double autocorr_4;
    double autocorr_8;
    double autocorr_16;
    double autocorr_20;
    double has_brackets;
    int n;
} BlockStats;

/*
 * Compute every statistic in a single sweep over memory.
 * One pass for the histogram + 5 passes for autocorrelation.
 *
 * NOTE: For inputs > 64 KB we sub-sample (16 KB total) so detection cost
 * stays roughly constant in input size.
 */
static int log2_table_init = 0;
static double log2_table[65537];  /* log2(k) for k in 1..65536 */

static void init_log2_table(void) {
    if (log2_table_init) return;
    log2_table[0] = 0;
    for (int i = 1; i <= 65536; i++) {
        log2_table[i] = log2((double)i);
    }
    log2_table_init = 1;
}

void analyze_block(const uint8_t *data, size_t n, BlockStats *out) {
    init_log2_table();
    out->n = (int)n;

    if (n == 0) {
        memset(((char*)out) + sizeof(int), 0, sizeof(BlockStats) - sizeof(int));
        return;
    }

    /* For large inputs, sub-sample (same pattern as the NumPy version):
     * 4 chunks of 4 KB each, evenly spaced over the input. */
    size_t sample_n;
    const uint8_t *sample;
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

    /* === Byte histogram === */
    uint32_t hist[256] = {0};
    uint32_t ascii_count = 0;
    uint32_t bracket_count = 0;
    for (size_t i = 0; i < sample_n; i++) {
        uint8_t b = sample[i];
        hist[b]++;
        if (b >= 32 && b < 127) ascii_count++;
        if (b == '{' || b == '}' || b == '[' || b == ']' ||
            b == '<' || b == '>' || b == '"' || b == ',' || b == ':') {
            bracket_count++;
        }
    }

    /* Entropy + most_common */
    double H = 0.0;
    double inv_n = 1.0 / (double)sample_n;
    uint32_t max_count = 0;
    double log2_n = log2((double)sample_n);
    for (int i = 0; i < 256; i++) {
        if (hist[i] > 0) {
            double p = (double)hist[i] * inv_n;
            /* log2(c/n) = log2(c) - log2(n) */
            uint32_t c = hist[i];
            double log2_c;
            if (c <= 65536) log2_c = log2_table[c];
            else log2_c = log2((double)c);
            H -= p * (log2_c - log2_n);
            if (c > max_count) max_count = c;
        }
    }
    out->H = H;
    out->asciiness = (double)ascii_count * inv_n;
    out->most_common_freq = (double)max_count * inv_n;
    out->has_brackets = (double)bracket_count * inv_n;

    /* === Autocorrelation at strides 2, 4, 8, 16, 20 ===
     * AC(k) = (P(arr[i] == arr[i+k]) - 1/256) * 256/255
     *       = (matches/n - 1/256) * 256/255
     */
    static const int ks[] = {2, 4, 8, 16, 20};
    double *outs[] = {&out->autocorr_2, &out->autocorr_4,
                      &out->autocorr_8, &out->autocorr_16, &out->autocorr_20};
    for (int j = 0; j < 5; j++) {
        int k = ks[j];
        if ((size_t)k >= sample_n) {
            *outs[j] = 0.0;
            continue;
        }
        size_t pairs = sample_n - k;
        size_t matches = 0;
        const uint8_t *a = sample;
        const uint8_t *b = sample + k;
        /* GCC vectorises this loop with AVX2 under -march=native. */
        for (size_t i = 0; i < pairs; i++) {
            if (a[i] == b[i]) matches++;
        }
        double m = (double)matches / (double)pairs;
        double normalized = (m - 1.0/256.0) * (256.0/255.0);
        if (normalized < 0) normalized = 0;
        *outs[j] = normalized;
    }
}

/*
 * Niche decision. Same logic as the inline detector in pipeline.c.
 */
int detect_niche_c(const uint8_t *data, size_t n) {
    BlockStats s;
    analyze_block(data, n, &s);

    if (n < 64) return NICHE_GENERIC;

    /* 1. CONSTANT */
    if (s.most_common_freq > 0.95) return NICHE_CONSTANT;

    /* 2. STRONG PATTERN + LOW ENTROPY -> generic (zstd crushes it). */
    double max_ac = s.autocorr_2;
    if (s.autocorr_4 > max_ac) max_ac = s.autocorr_4;
    if (s.autocorr_8 > max_ac) max_ac = s.autocorr_8;
    if (s.autocorr_16 > max_ac) max_ac = s.autocorr_16;
    if (max_ac > 0.9 && s.H < 5.5) return NICHE_GENERIC;

    /* 3. RANDOM */
    if (s.H > 7.7 && s.autocorr_2 < 0.02 && s.autocorr_4 < 0.02 &&
            s.autocorr_8 < 0.02 && s.autocorr_16 < 0.02) {
        return NICHE_RANDOM;
    }

    /* 4a. Integer arrays with constant high-order bytes.
     * The smallest k with AC > 0.4 (among 2, 4, 8) is the "real" element size. */
    int has_2 = s.autocorr_2 > 0.4;
    int has_4 = s.autocorr_4 > 0.4;
    int has_8 = s.autocorr_8 > 0.4;
    if (has_2 || has_4 || has_8) {
        double max_v = 0;
        if (has_2 && s.autocorr_2 > max_v) max_v = s.autocorr_2;
        if (has_4 && s.autocorr_4 > max_v) max_v = s.autocorr_4;
        if (has_8 && s.autocorr_8 > max_v) max_v = s.autocorr_8;
        /* Prefer the smallest k whose AC is within 80% of the max. */
        if (has_2 && s.autocorr_2 >= max_v * 0.8) return NICHE_NUMERIC_I16;
        if (has_4 && s.autocorr_4 >= max_v * 0.8) return NICHE_NUMERIC_I32;
        if (has_8 && s.autocorr_8 >= max_v * 0.8) return NICHE_NUMERIC_I64;
    }

    /* 4b. Noisy floats (moderate AC). */
    if (s.H > 6.0) {
        struct { int k; double v; int niche; } cands[5] = {
            {2, s.autocorr_2, NICHE_NUMERIC_I16},
            {4, s.autocorr_4, NICHE_NUMERIC_F32},
            {8, s.autocorr_8, NICHE_NUMERIC_F64},
            {16, s.autocorr_16, NICHE_SHUFFLE_16},
            {20, s.autocorr_20, NICHE_SHUFFLE_20},
        };
        int best = -1;
        double best_v = 0.08;
        for (int i = 0; i < 5; i++) {
            if (cands[i].v > best_v) {
                best_v = cands[i].v;
                best = i;
            }
        }
        if (best >= 0) return cands[best].niche;
    }

    /* 4c. int16 audio. */
    if (n % 2 == 0 && s.H > 7.0 && s.asciiness < 0.5
            && s.autocorr_2 > 0.005 && s.autocorr_2 < 0.1) {
        return NICHE_NUMERIC_I16;
    }

    /* 4d. Low-cardinality floats. */
    if (n % 8 == 0 && s.H > 3.0 && s.H < 6.0 && s.asciiness < 0.7
            && s.autocorr_8 > 0.15 && max_ac < 0.85) {
        return NICHE_NUMERIC_F64;
    }

    /* 5. STRUCTURED */
    if (s.asciiness > 0.85 && s.has_brackets > 0.05) return NICHE_STRUCTURED;

    /* 6. TEXT */
    if (s.asciiness > 0.85) return NICHE_TEXT;

    return NICHE_GENERIC;
}

/* Convenience wrapper for callers that want the raw stats too. */
void analyze_block_export(const uint8_t *data, size_t n, BlockStats *out) {
    analyze_block(data, n, out);
}
