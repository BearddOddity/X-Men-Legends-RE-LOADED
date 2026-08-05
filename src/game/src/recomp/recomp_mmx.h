/**
 * MMX register file and packed-integer operations.
 *
 * Why this exists
 * ---------------
 * The lifter emitted every MMX instruction as a bare C comment - 720 sites.
 * That does not fail loudly; it leaves the destination holding whatever was
 * there before, so the game computes with stale data and produces wrong
 * results far from the cause. These are the instructions the XDK uses for
 * colour conversion, texture packing and audio mixing, so the symptoms would
 * be wrong colours, warped geometry and garbled sound rather than a crash.
 *
 * The Xbox CPU is a Coppermine Pentium III: MMX and SSE, no SSE2. So MMX is
 * genuinely used, not a dead CPU-dispatch path (unlike the 3DNow! sites, which
 * an Intel part can never reach - see recomp_cpuid in recomp_manual.c).
 *
 * Register model
 * --------------
 * On real hardware MMX aliases the x87 register stack, which is why code must
 * issue EMMS before returning to float work. This recomp models x87 separately
 * (fp_push/fp_top in the generated code), so aliasing would buy nothing but
 * bugs - the eight MMX registers are kept independent here and EMMS stays a
 * no-op. Any program that depends on reading MMX data back through x87 would
 * break, but that is a trick no compiler emits.
 *
 * Everything is a 64-bit value operated on as packed lanes. Helpers are
 * `static inline` and take/return uint64_t by value, so the generated code
 * reads as `mm0 = mmx_paddw(mm0, mm1);`.
 *
 * The registers themselves are ordinary uint64_t locals declared per function
 * by the translator - unlike the x87 stack, which had to become global because
 * float return values cross call boundaries in st(0). MMX values never do:
 * the ABI requires EMMS before any call that touches float state, so every
 * MMX sequence begins and ends inside one function.
 */
#ifndef RECOMP_MMX_H
#define RECOMP_MMX_H

#include <stdint.h>

/* ── lane access ─────────────────────────────────────────────── */

static inline uint16_t mmx_w(uint64_t v, int i) { return (uint16_t)(v >> (i * 16)); }
static inline uint32_t mmx_d(uint64_t v, int i) { return (uint32_t)(v >> (i * 32)); }
static inline uint8_t  mmx_b(uint64_t v, int i) { return (uint8_t)(v >> (i * 8)); }

static inline uint64_t mmx_pack_w(uint16_t a, uint16_t b, uint16_t c, uint16_t d)
{
    return (uint64_t)a | ((uint64_t)b << 16) | ((uint64_t)c << 32)
         | ((uint64_t)d << 48);
}

/* ── saturation ──────────────────────────────────────────────── */

static inline uint8_t  mmx_sat_u8(int32_t v)  { return v < 0 ? 0 : (v > 255 ? 255 : (uint8_t)v); }
static inline int16_t  mmx_sat_s16(int32_t v) { return v < -32768 ? -32768 : (v > 32767 ? 32767 : (int16_t)v); }
static inline uint16_t mmx_sat_u16(int32_t v) { return v < 0 ? 0 : (v > 65535 ? 65535 : (uint16_t)v); }

/* ── arithmetic ──────────────────────────────────────────────── */

/* paddw - four 16-bit adds, wrapping */
static inline uint64_t mmx_paddw(uint64_t a, uint64_t b)
{
    return mmx_pack_w((uint16_t)(mmx_w(a,0) + mmx_w(b,0)),
                      (uint16_t)(mmx_w(a,1) + mmx_w(b,1)),
                      (uint16_t)(mmx_w(a,2) + mmx_w(b,2)),
                      (uint16_t)(mmx_w(a,3) + mmx_w(b,3)));
}

/* paddusw - four 16-bit unsigned adds, saturating */
static inline uint64_t mmx_paddusw(uint64_t a, uint64_t b)
{
    return mmx_pack_w(mmx_sat_u16((int32_t)mmx_w(a,0) + mmx_w(b,0)),
                      mmx_sat_u16((int32_t)mmx_w(a,1) + mmx_w(b,1)),
                      mmx_sat_u16((int32_t)mmx_w(a,2) + mmx_w(b,2)),
                      mmx_sat_u16((int32_t)mmx_w(a,3) + mmx_w(b,3)));
}

/* psubsw - four 16-bit signed subtracts, saturating */
static inline uint64_t mmx_psubsw(uint64_t a, uint64_t b)
{
    return mmx_pack_w((uint16_t)mmx_sat_s16((int16_t)mmx_w(a,0) - (int16_t)mmx_w(b,0)),
                      (uint16_t)mmx_sat_s16((int16_t)mmx_w(a,1) - (int16_t)mmx_w(b,1)),
                      (uint16_t)mmx_sat_s16((int16_t)mmx_w(a,2) - (int16_t)mmx_w(b,2)),
                      (uint16_t)mmx_sat_s16((int16_t)mmx_w(a,3) - (int16_t)mmx_w(b,3)));
}

/* pmullw - four 16-bit multiplies, keeping the low half */
static inline uint64_t mmx_pmullw(uint64_t a, uint64_t b)
{
    return mmx_pack_w((uint16_t)((int16_t)mmx_w(a,0) * (int16_t)mmx_w(b,0)),
                      (uint16_t)((int16_t)mmx_w(a,1) * (int16_t)mmx_w(b,1)),
                      (uint16_t)((int16_t)mmx_w(a,2) * (int16_t)mmx_w(b,2)),
                      (uint16_t)((int16_t)mmx_w(a,3) * (int16_t)mmx_w(b,3)));
}

/* pavgw / pavgb - unsigned average, rounding up */
static inline uint64_t mmx_pavgw(uint64_t a, uint64_t b)
{
    return mmx_pack_w((uint16_t)((mmx_w(a,0) + mmx_w(b,0) + 1) >> 1),
                      (uint16_t)((mmx_w(a,1) + mmx_w(b,1) + 1) >> 1),
                      (uint16_t)((mmx_w(a,2) + mmx_w(b,2) + 1) >> 1),
                      (uint16_t)((mmx_w(a,3) + mmx_w(b,3) + 1) >> 1));
}

static inline uint64_t mmx_pavgb(uint64_t a, uint64_t b)
{
    uint64_t r = 0;
    for (int i = 0; i < 8; i++) {
        r |= (uint64_t)(uint8_t)((mmx_b(a,i) + mmx_b(b,i) + 1) >> 1) << (i * 8);
    }
    return r;
}

/* ── shifts ──────────────────────────────────────────────────── */
/* Counts >= the lane width clear the lanes - that is the hardware rule, and
 * getting it wrong here would look like a shift working "sometimes". */

static inline uint64_t mmx_psraw(uint64_t a, uint64_t cnt)
{
    int c = cnt > 15 ? 15 : (int)cnt;        /* arithmetic: saturates to sign */
    return mmx_pack_w((uint16_t)((int16_t)mmx_w(a,0) >> c),
                      (uint16_t)((int16_t)mmx_w(a,1) >> c),
                      (uint16_t)((int16_t)mmx_w(a,2) >> c),
                      (uint16_t)((int16_t)mmx_w(a,3) >> c));
}

static inline uint64_t mmx_psrlw(uint64_t a, uint64_t cnt)
{
    if (cnt > 15) return 0;
    int c = (int)cnt;
    return mmx_pack_w((uint16_t)(mmx_w(a,0) >> c), (uint16_t)(mmx_w(a,1) >> c),
                      (uint16_t)(mmx_w(a,2) >> c), (uint16_t)(mmx_w(a,3) >> c));
}

static inline uint64_t mmx_psllw(uint64_t a, uint64_t cnt)
{
    if (cnt > 15) return 0;
    int c = (int)cnt;
    return mmx_pack_w((uint16_t)(mmx_w(a,0) << c), (uint16_t)(mmx_w(a,1) << c),
                      (uint16_t)(mmx_w(a,2) << c), (uint16_t)(mmx_w(a,3) << c));
}

static inline uint64_t mmx_psrlq(uint64_t a, uint64_t cnt)
{
    return cnt > 63 ? 0 : (a >> cnt);
}

static inline uint64_t mmx_psllq(uint64_t a, uint64_t cnt)
{
    return cnt > 63 ? 0 : (a << cnt);
}

static inline uint64_t mmx_psrld(uint64_t a, uint64_t cnt)
{
    if (cnt > 31) return 0;
    return (uint64_t)(mmx_d(a,0) >> cnt) | ((uint64_t)(mmx_d(a,1) >> cnt) << 32);
}

static inline uint64_t mmx_pslld(uint64_t a, uint64_t cnt)
{
    if (cnt > 31) return 0;
    return (uint64_t)(mmx_d(a,0) << cnt) | ((uint64_t)(mmx_d(a,1) << cnt) << 32);
}

/* ── pack / unpack ───────────────────────────────────────────── */

/* packuswb - eight signed 16-bit lanes -> eight unsigned bytes, saturating.
 * dst supplies the low four, src the high four. */
static inline uint64_t mmx_packuswb(uint64_t a, uint64_t b)
{
    uint64_t r = 0;
    for (int i = 0; i < 4; i++)
        r |= (uint64_t)mmx_sat_u8((int16_t)mmx_w(a, i)) << (i * 8);
    for (int i = 0; i < 4; i++)
        r |= (uint64_t)mmx_sat_u8((int16_t)mmx_w(b, i)) << ((i + 4) * 8);
    return r;
}

/* packssdw - four signed 32-bit lanes -> four signed 16-bit, saturating */
static inline uint64_t mmx_packssdw(uint64_t a, uint64_t b)
{
    return mmx_pack_w((uint16_t)mmx_sat_s16((int32_t)mmx_d(a,0)),
                      (uint16_t)mmx_sat_s16((int32_t)mmx_d(a,1)),
                      (uint16_t)mmx_sat_s16((int32_t)mmx_d(b,0)),
                      (uint16_t)mmx_sat_s16((int32_t)mmx_d(b,1)));
}

/* packsswb - eight signed 16-bit -> eight signed bytes, saturating */
static inline uint64_t mmx_packsswb(uint64_t a, uint64_t b)
{
    uint64_t r = 0;
    for (int i = 0; i < 4; i++) {
        int32_t v = (int16_t)mmx_w(a, i);
        int8_t s = v < -128 ? -128 : (v > 127 ? 127 : (int8_t)v);
        r |= (uint64_t)(uint8_t)s << (i * 8);
    }
    for (int i = 0; i < 4; i++) {
        int32_t v = (int16_t)mmx_w(b, i);
        int8_t s = v < -128 ? -128 : (v > 127 ? 127 : (int8_t)v);
        r |= (uint64_t)(uint8_t)s << ((i + 4) * 8);
    }
    return r;
}

/* punpck* - interleave lanes from dst (a) and src (b) */
static inline uint64_t mmx_punpcklbw(uint64_t a, uint64_t b)
{
    uint64_t r = 0;
    for (int i = 0; i < 4; i++) {
        r |= (uint64_t)mmx_b(a, i) << (i * 16);
        r |= (uint64_t)mmx_b(b, i) << (i * 16 + 8);
    }
    return r;
}

static inline uint64_t mmx_punpckhbw(uint64_t a, uint64_t b)
{
    uint64_t r = 0;
    for (int i = 0; i < 4; i++) {
        r |= (uint64_t)mmx_b(a, i + 4) << (i * 16);
        r |= (uint64_t)mmx_b(b, i + 4) << (i * 16 + 8);
    }
    return r;
}

static inline uint64_t mmx_punpcklwd(uint64_t a, uint64_t b)
{
    return mmx_pack_w(mmx_w(a,0), mmx_w(b,0), mmx_w(a,1), mmx_w(b,1));
}

static inline uint64_t mmx_punpckhwd(uint64_t a, uint64_t b)
{
    return mmx_pack_w(mmx_w(a,2), mmx_w(b,2), mmx_w(a,3), mmx_w(b,3));
}

static inline uint64_t mmx_punpckldq(uint64_t a, uint64_t b)
{
    return (uint64_t)mmx_d(a,0) | ((uint64_t)mmx_d(b,0) << 32);
}

static inline uint64_t mmx_punpckhdq(uint64_t a, uint64_t b)
{
    return (uint64_t)mmx_d(a,1) | ((uint64_t)mmx_d(b,1) << 32);
}

/* ── logic and compare ───────────────────────────────────────── */

static inline uint64_t mmx_pand(uint64_t a, uint64_t b)  { return a & b; }
static inline uint64_t mmx_pandn(uint64_t a, uint64_t b) { return (~a) & b; }
static inline uint64_t mmx_por(uint64_t a, uint64_t b)   { return a | b; }
static inline uint64_t mmx_pxor(uint64_t a, uint64_t b)  { return a ^ b; }

static inline uint64_t mmx_pcmpgtd(uint64_t a, uint64_t b)
{
    uint64_t lo = ((int32_t)mmx_d(a,0) > (int32_t)mmx_d(b,0)) ? 0xFFFFFFFFu : 0;
    uint64_t hi = ((int32_t)mmx_d(a,1) > (int32_t)mmx_d(b,1)) ? 0xFFFFFFFFu : 0;
    return lo | (hi << 32);
}

static inline uint64_t mmx_pcmpgtw(uint64_t a, uint64_t b)
{
    return mmx_pack_w(((int16_t)mmx_w(a,0) > (int16_t)mmx_w(b,0)) ? 0xFFFFu : 0,
                      ((int16_t)mmx_w(a,1) > (int16_t)mmx_w(b,1)) ? 0xFFFFu : 0,
                      ((int16_t)mmx_w(a,2) > (int16_t)mmx_w(b,2)) ? 0xFFFFu : 0,
                      ((int16_t)mmx_w(a,3) > (int16_t)mmx_w(b,3)) ? 0xFFFFu : 0);
}

static inline uint64_t mmx_pcmpeqw(uint64_t a, uint64_t b)
{
    return mmx_pack_w(mmx_w(a,0) == mmx_w(b,0) ? 0xFFFFu : 0,
                      mmx_w(a,1) == mmx_w(b,1) ? 0xFFFFu : 0,
                      mmx_w(a,2) == mmx_w(b,2) ? 0xFFFFu : 0,
                      mmx_w(a,3) == mmx_w(b,3) ? 0xFFFFu : 0);
}

/* ── 32-bit lane arithmetic ──────────────────────────────────── */

/* paddd - two 32-bit adds, wrapping */
static inline uint64_t mmx_paddd(uint64_t a, uint64_t b)
{
    uint64_t lo = (uint32_t)(mmx_d(a,0) + mmx_d(b,0));
    uint64_t hi = (uint32_t)(mmx_d(a,1) + mmx_d(b,1));
    return lo | (hi << 32);
}

/* psubd - two 32-bit subtracts, wrapping */
static inline uint64_t mmx_psubd(uint64_t a, uint64_t b)
{
    uint64_t lo = (uint32_t)(mmx_d(a,0) - mmx_d(b,0));
    uint64_t hi = (uint32_t)(mmx_d(a,1) - mmx_d(b,1));
    return lo | (hi << 32);
}

/* pmaddwd - multiply four signed 16-bit pairs, then add adjacent products
 * into two signed 32-bit lanes. Products are computed in 32 bits; the sum
 * wraps rather than saturating, matching hardware.
 *
 * The one case that is not merely wrap-on-overflow is
 * (-32768 * -32768) + (-32768 * -32768) = 0x80000000, and hardware
 * produces exactly that, so no special case is needed. */
static inline uint64_t mmx_pmaddwd(uint64_t a, uint64_t b)
{
    int32_t p0 = (int32_t)(int16_t)mmx_w(a,0) * (int32_t)(int16_t)mmx_w(b,0);
    int32_t p1 = (int32_t)(int16_t)mmx_w(a,1) * (int32_t)(int16_t)mmx_w(b,1);
    int32_t p2 = (int32_t)(int16_t)mmx_w(a,2) * (int32_t)(int16_t)mmx_w(b,2);
    int32_t p3 = (int32_t)(int16_t)mmx_w(a,3) * (int32_t)(int16_t)mmx_w(b,3);
    uint64_t lo = (uint32_t)((uint32_t)p0 + (uint32_t)p1);
    uint64_t hi = (uint32_t)((uint32_t)p2 + (uint32_t)p3);
    return lo | (hi << 32);
}

/* ── extract ─────────────────────────────────────────────────── */

/* pextrw r32, mm, imm8 - the index is taken modulo 4, per the ISA */
static inline uint32_t mmx_pextrw(uint64_t a, int sel)
{
    return mmx_w(a, sel & 3);
}

#endif /* RECOMP_MMX_H */
