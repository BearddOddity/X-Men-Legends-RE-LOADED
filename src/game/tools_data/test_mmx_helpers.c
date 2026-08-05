/*
 * Self-check for the packed-integer helpers in src/recomp/recomp_mmx.h.
 *
 * These are pure functions with no dependency on the Xbox memory map, so
 * they can be compiled and run standalone. Saturation boundaries and lane
 * ordering are the parts that are easy to get subtly wrong and that no
 * build error would ever catch.
 *
 * Build and run (from src/game/):
 *   cl /nologo /I src/recomp tools_data/test_mmx_helpers.c && test_mmx_helpers.exe
 *   gcc -I src/recomp -o test_mmx tools_data/test_mmx_helpers.c && ./test_mmx
 */
#include <stdio.h>
#include <stdint.h>

#include "recomp_mmx.h"

static int failures = 0;

#define CHECK(expr, want)                                                     \
    do {                                                                      \
        uint64_t _got = (uint64_t)(expr);                                     \
        uint64_t _want = (uint64_t)(want);                                    \
        if (_got != _want) {                                                  \
            printf("FAIL %s:%d  %s\n     got  0x%016llX\n     want 0x%016llX\n", \
                   __FILE__, __LINE__, #expr,                                 \
                   (unsigned long long)_got, (unsigned long long)_want);      \
            failures++;                                                       \
        }                                                                     \
    } while (0)

/* Build a 64-bit value from four 16-bit lanes, lane 0 lowest. */
#define W4(a, b, c, d) \
    ((uint64_t)(uint16_t)(a) | ((uint64_t)(uint16_t)(b) << 16) | \
     ((uint64_t)(uint16_t)(c) << 32) | ((uint64_t)(uint16_t)(d) << 48))

int main(void)
{
    /* ── lane order ───────────────────────────────────────────── */
    /* Lane 0 must be the LOW half. Getting this backwards silently
     * mirrors every pixel these routines touch. */
    CHECK(mmx_w(0x1111222233334444ULL, 0), 0x4444);
    CHECK(mmx_w(0x1111222233334444ULL, 3), 0x1111);
    CHECK(mmx_b(0x0102030405060708ULL, 0), 0x08);
    CHECK(mmx_d(0xAAAABBBBCCCCDDDDULL, 0), 0xCCCCDDDDu);

    /* ── wrapping arithmetic ──────────────────────────────────── */
    CHECK(mmx_paddw(W4(1, 2, 3, 4), W4(10, 20, 30, 40)),
          W4(11, 22, 33, 44));
    /* 16-bit add must wrap, not saturate. */
    CHECK(mmx_paddw(W4(0xFFFF, 0, 0, 0), W4(1, 0, 0, 0)), W4(0, 0, 0, 0));

    CHECK(mmx_paddd(0x00000001FFFFFFFFULL, 0x0000000100000001ULL),
          0x0000000200000000ULL);

    /* ── saturation ───────────────────────────────────────────── */
    /* paddusw clamps at 0xFFFF rather than wrapping. */
    CHECK(mmx_paddusw(W4(0xFFF0, 5, 0, 0), W4(0x0020, 5, 0, 0)),
          W4(0xFFFF, 10, 0, 0));
    /* psubsw clamps at the signed limits both ways. */
    CHECK(mmx_psubsw(W4(0x8000, 0x7FFF, 5, 0), W4(1, (uint16_t)-1, 3, 0)),
          W4(0x8000, 0x7FFF, 2, 0));

    /* packuswb takes signed words to unsigned bytes: negative -> 0,
     * above 255 -> 255. a supplies the low four bytes, b the high four.
     *   a = [-1, 0, 300, 255] -> bytes 00 00 FF FF
     *   b = [ 1, 2,   3,   4] -> bytes 01 02 03 04
     * and byte 0 is the low byte of the result. */
    CHECK(mmx_packuswb(W4(-1, 0, 300, 255), W4(1, 2, 3, 4)),
          0x04030201FFFF0000ULL);

    /* ── multiply ─────────────────────────────────────────────── */
    CHECK(mmx_pmullw(W4(2, 3, 4, 5), W4(10, 10, 10, 10)),
          W4(20, 30, 40, 50));
    /* pmullw keeps the LOW 16 bits of the product. */
    CHECK(mmx_pmullw(W4(0x1000, 0, 0, 0), W4(0x0010, 0, 0, 0)),
          W4(0x0000, 0, 0, 0));

    /* pmaddwd: products of adjacent signed pairs, summed into 32-bit lanes.
     * (2*3 + 4*5) = 26 low, (1*1 + 2*2) = 5 high. */
    CHECK(mmx_pmaddwd(W4(2, 4, 1, 2), W4(3, 5, 1, 2)),
          ((uint64_t)5 << 32) | 26);
    /* Negative operands must use signed multiplication. */
    CHECK(mmx_pmaddwd(W4(-1, 0, 0, 0), W4(2, 0, 0, 0)),
          (uint64_t)(uint32_t)-2);

    /* ── shifts ───────────────────────────────────────────────── */
    CHECK(mmx_psrlq(0xFF00000000000000ULL, 32), 0x00000000FF000000ULL);
    CHECK(mmx_psllq(0x00000000000000FFULL, 8),  0x000000000000FF00ULL);
    /* psraw is an ARITHMETIC shift - the sign bit must replicate. */
    CHECK(mmx_psraw(W4(0x8000, 0x4000, 0, 0), 1), W4(0xC000, 0x2000, 0, 0));
    CHECK(mmx_psrlw(W4(0x8000, 0, 0, 0), 1), W4(0x4000, 0, 0, 0));
    /* A count at or past the lane width clears the lanes, per hardware. */
    CHECK(mmx_psrlw(W4(0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF), 16), 0);
    CHECK(mmx_psllw(W4(0xFFFF, 0, 0, 0), 99), 0);

    /* ── unpack ───────────────────────────────────────────────── */
    /* punpcklbw interleaves the LOW four bytes, a first. */
    CHECK(mmx_punpcklbw(0x0000000004030201ULL, 0x00000000A0B0C0D0ULL),
          0xA004B003C002D001ULL);
    CHECK(mmx_punpckldq(0x1111111122222222ULL, 0x3333333344444444ULL),
          0x4444444422222222ULL);
    CHECK(mmx_punpckhdq(0x1111111122222222ULL, 0x3333333344444444ULL),
          0x3333333311111111ULL);

    /* ── logic and compare ────────────────────────────────────── */
    /* pandn is (~a) & b, NOT a & ~b. Getting it backwards is a classic. */
    CHECK(mmx_pandn(0xF0F0F0F0F0F0F0F0ULL, 0xFFFFFFFFFFFFFFFFULL),
          0x0F0F0F0F0F0F0F0FULL);
    CHECK(mmx_pxor(0x5555555555555555ULL, 0xFFFFFFFFFFFFFFFFULL),
          0xAAAAAAAAAAAAAAAAULL);
    /* pcmpgtw is a SIGNED compare: -1 is not greater than 1. */
    CHECK(mmx_pcmpgtw(W4(-1, 5, 0, 0), W4(1, 1, 0, 0)),
          W4(0, 0xFFFF, 0, 0));

    /* ── extract ──────────────────────────────────────────────── */
    CHECK(mmx_pextrw(0x1111222233334444ULL, 1), 0x3333);
    /* The selector is taken modulo 4. */
    CHECK(mmx_pextrw(0x1111222233334444ULL, 5), 0x3333);

    if (failures) {
        printf("\n%d check(s) FAILED\n", failures);
        return 1;
    }
    printf("mmx helper self-check: all passed\n");
    return 0;
}
