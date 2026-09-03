/**
 * Xbox Static Recompilation - Runtime Type Definitions
 *
 * Type definitions and helper macros used by mechanically translated
 * x86 -> C code. Each original x86 function is translated to a C
 * function that uses these types and macros.
 *
 * This is a reusable template for ANY Xbox game. Game-specific
 * customization should go in separate headers.
 *
 * Memory model:
 *   Xbox data sections are mapped to their original VAs via
 *   CreateFileMapping + MapViewOfFileEx (see xbox_memory.h).
 *   Recompiled code accesses globals via pointer casts, e.g.:
 *     *(uint32_t*)0x003B2360
 *
 * Register model:
 *   Volatile registers (eax, ecx, edx, esp) are global variables,
 *   matching real x86 behavior where these registers are shared
 *   across all code. This enables correct argument passing via the
 *   simulated stack and return value communication via eax.
 *
 *   Callee-saved registers (ebx, esi, edi) are also global because
 *   callers pass implicit parameters through them (e.g. 'this' via
 *   esi in thiscall). The callee-save contract is enforced by
 *   PUSH32/POP32 instructions in the generated code, not by C local
 *   variable scoping.
 *
 *   ebp is NOT global - it stays local in each function because many
 *   FPO (Frame Pointer Omission) functions use it as scratch without
 *   save/restore. For SEH functions, g_seh_ebp bridges the gap.
 *
 * Calling convention:
 *   All translated functions are void(void). Arguments are passed
 *   on the simulated Xbox stack (via push instructions before call).
 *   Return values are communicated through g_eax.
 *   The call instruction pushes a dummy return address; ret pops it.
 */

#ifndef RECOMP_TYPES_H
#define RECOMP_TYPES_H

#include <stdint.h>
#include <stddef.h>
#include <string.h>

/* Thread-local qualifier for the simulated register file.
 *
 * The x86 register file belongs to a CPU thread, not to a process. Modelling
 * it as plain globals was only correct while the port had a single thread of
 * execution - which it did, because PsCreateSystemThreadEx ran every start
 * routine synchronously. Real threads are impossible until this is per-thread:
 * two threads sharing g_esp corrupt each other on the first push.
 *
 * EVERY declaration of these must carry the qualifier. main.c used to declare
 * its own copies without it; MSVC accepts the mismatch across translation
 * units and silently resolves the reference to the image's read-only TLS
 * template, so the first write faulted. That cost a full revert cycle.
 */
#ifndef RECOMP_TLS
#if defined(_MSC_VER)
#define RECOMP_TLS __declspec(thread)
#else
#define RECOMP_TLS _Thread_local
#endif
#endif

/* MSVC's __forceinline -> gcc/clang equivalent on POSIX. */
#if !defined(_MSC_VER) && !defined(__forceinline)
#define __forceinline inline __attribute__((always_inline))
#endif

/* MSVC's __debugbreak() intrinsic -> gcc/clang equivalent.
 * The auto-generated code emits __debugbreak for x86 INT 3 instructions. */
#if !defined(_MSC_VER) && !defined(__debugbreak)
#define __debugbreak() __builtin_trap()
#endif

/* ================================================================
 * Memory offset
 * ================================================================ */

/**
 * Memory offset from Xbox VA to actual mapped address.
 * When Xbox memory is mapped at the original address (0x00010000),
 * this is 0 and the MEM macros are simple identity casts.
 * When mapped elsewhere, this adjusts all memory accesses.
 *
 * Set once during memory initialization, then read-only.
 */
extern ptrdiff_t g_xbox_mem_offset;

/* ================================================================
 * Global registers
 * ================================================================ */

/**
 * Volatile x86 registers (caller-saved):
 *   eax - return values, general accumulator
 *   ecx - 'this' pointer for thiscall, loop counter
 *   edx - high dword of multiply/divide, general
 *   esp - stack pointer (initialized to top of Xbox stack)
 *
 * Callee-saved x86 registers (also global):
 *   ebx, esi, edi - global because callers pass implicit parameters
 *   through them. The callee-save contract is enforced by generated
 *   PUSH32/POP32 instructions.
 *
 * NOT global: ebp - stays local in each function because FPO
 * functions use it as scratch. For SEH, g_seh_ebp bridges the gap.
 */
extern RECOMP_TLS uint32_t g_eax, g_ecx, g_edx, g_esp;
extern RECOMP_TLS uint32_t g_ebx, g_esi, g_edi;

/**
 * x87 stack. Global because the FPU is a machine resource, not a per-frame
 * one: a function returning float/double leaves it in st(0) for the caller
 * to pop. These were function locals, so every float return value was lost
 * at the call boundary and the caller popped an uninitialised slot.
 *
 * g_fp_top is the TOP field: 0 after finit, predecremented on push.
 */
extern RECOMP_TLS double g_fp_stack[8];
extern RECOMP_TLS int    g_fp_top;

/**
 * x87 control word. Stored so the save/set/restore idiom around fistp
 * round-trips faithfully. The rounding-mode field is NOT acted on:
 * fist/fistp use C's truncate-toward-zero, which is the mode that idiom
 * selects anyway. 0x037F is the value after finit.
 */
extern RECOMP_TLS uint16_t g_fp_cw;

/**
 * SEH frame pointer bridge.
 *
 * __SEH_prolog sets up ebp for the caller, but since ebp is a local
 * variable in each function, the caller can't see the prolog's change.
 * The prolog writes g_seh_ebp, and the caller reads it after the call.
 * Similarly, __SEH_epilog reads g_seh_ebp at entry and writes it at exit.
 */
extern RECOMP_TLS uint32_t g_seh_ebp;

/**
 * Base this thread's fs: resolves to - its TIB.
 *
 * Segment overrides were discarded by the lifter, so fs:[0] became MEM32(0)
 * and one fake TIB at VA 0 served the whole process. That holds only while
 * there is a single thread: the SEH chain head at fs:[0] is pushed by every
 * __SEH_prolog and popped by every __SEH_epilog, so two threads sharing it
 * would splice their exception chains into each other.
 *
 * Zero until per-thread TIBs are allocated, so MEM32(g_fs_base + X) is
 * exactly the MEM32(X) that was emitted before.
 */
extern RECOMP_TLS uint32_t g_fs_base;

/* ================================================================
 * ICALL trace ring buffer (for debugging indirect calls)
 * ================================================================ */

/** Size of the ring buffer (must be power of 2). */
#define ICALL_TRACE_SIZE 16

/** Ring buffer of recent indirect call target VAs. */
extern volatile uint32_t g_icall_trace[ICALL_TRACE_SIZE];

/** Current write index into the ring buffer. */
extern volatile uint32_t g_icall_trace_idx;

/** Total count of indirect calls executed. */
extern volatile uint64_t g_icall_count;

/** Size of the allocator-return ring (must be power of 2; storage in
 *  xbox_memory_layout.c spells this as a literal, keep the two in step). */
#define ALLOC_TRACE_SIZE 1024

/** Ring buffer of addresses returned by the engine allocator.
 *
 *  Written with two plain stores and NO call - see the long note at the
 *  storage definition. The point is to re-measure ledger #16's duplicate
 *  returns without the C call that #17 proved kills the boot. */
extern volatile uint32_t g_alloc_trace[ALLOC_TRACE_SIZE];

/** Current write index into the allocator ring. Also the total allocation
 *  count, since it only ever increments. */
extern volatile uint32_t g_alloc_trace_idx;

/**
 * Called when an indirect call target cannot be resolved.
 * Implement this in your game-specific code to log diagnostics.
 * The va parameter is the Xbox VA that failed to resolve.
 */
void recomp_icall_fail_log(uint32_t va);

/* Safe ICALL stub - called when an indirect call target is invalid.
 * Instead of crashing by calling 0, this returns S_OK (0) safely. */
void sub_00ICALL_SAFE_STUB(void);

/**
 * Record an indirect-call target rejected by the plausibility filter.
 *
 * The [0x00400000, 0xFE000000) rejection used to be silent, so a spin could
 * burn 80k calls through the safe stub without ever naming the target.
 * Keeps a histogram and prints each distinct target once. Implemented in
 * recomp_manual.c; dumped by recomp_icall_reject_dump() at exit.
 */
void recomp_icall_reject_log(uint32_t va, const char *file, int line);
void recomp_icall_reject_dump(void);

/* ================================================================
 * Coverage: how much of the game actually ran
 * ================================================================ */

/**
 * Mark a guest code address as reached, and count DISTINCT addresses.
 *
 * Why this exists: kernel_calls, the metric every tool gates on, sat at exactly
 * 1452 through every experiment on 2026-08-06 - 566 seeded functions, 31 lifter
 * repairs, a freeze fixed. All of it read as "no change", because the number
 * saturates wherever the boot stops and says nothing about how much code ran
 * before that. Tools cannot hill-climb against a flat signal, so a whole day of
 * automated grinding produced 25 identical readings.
 *
 * Distinct reached addresses move whenever more of the engine executes, even
 * when the boot still stops in the same place. That is the difference between
 * a gate that can steer and one that cannot.
 *
 * A bitmap over the code range, one bit per 4-byte-aligned address: about
 * 128 KB, one shift and one OR on the hot path. Cheap enough for the 622
 * million dispatches a spin produces.
 *
 * It counts indirect-call targets, not every function - direct calls are
 * ordinary C calls with nothing to hook. In this engine that is a good proxy:
 * 18,556 generated call sites are indirect, because the whole thing is
 * virtual dispatch through igMetaObject.
 */
#define RECOMP_COVER_LO 0x00010000u
#define RECOMP_COVER_HI 0x00400000u
#define RECOMP_COVER_BITS ((RECOMP_COVER_HI - RECOMP_COVER_LO) / 4u)

extern uint8_t  g_reached[RECOMP_COVER_BITS / 8u];
extern uint32_t g_reached_count;

/*
 * Distinct DIRECT call sites executed - the blind spot in g_reached_count.
 *
 * The comment above calls indirect-only coverage "a good proxy". It stops
 * being one exactly when a fix removes indirect work. Seeding sub_00011B2B
 * and sub_001E9558 (ledger #72) cut a vtable-heavy retry spin out of the
 * allocator; kernel_calls went 82 -> 330 and the backtrace 4 -> 18 frames,
 * while g_reached_count FELL 55 -> 42, because the boot stopped re-resolving
 * the same vtable slots and started running straight-line code instead.
 * Judged on g_reached_count alone that fix reads as a regression.
 *
 * So count the other half too. One static flag per textual call site, set on
 * first execution: a predictable branch and one byte of .bss per site, and it
 * rises whenever code that was never entered before runs - including code
 * reached entirely by direct calls, which g_reached cannot see.
 *
 * This counts call SITES, not functions. That is deliberate and finer
 * grained: two call sites into one function are two distinct pieces of
 * control flow, and marking function entries instead would mean touching all
 * 30,002 generated bodies rather than one macro.
 *
 * Report BOTH numbers and read them together. Neither alone is progress.
 */
extern uint32_t g_callsite_count;

void recomp_coverage_dump(void);

/* Allocation duplicate detector - see recomp_manual.c. */
void recomp_alloc_log(uint32_t addr);
void recomp_alloc_dump(void);
uint32_t recomp_alloc_fixup(uint32_t addr, uint32_t size);

static inline void recomp_mark_reached(uint32_t va)
{
    if (va < RECOMP_COVER_LO || va >= RECOMP_COVER_HI) {
        return;
    }
    uint32_t bit = (va - RECOMP_COVER_LO) >> 2;
    uint8_t  msk = (uint8_t)(1u << (bit & 7u));
    uint8_t *cell = &g_reached[bit >> 3];
    if (!(*cell & msk)) {
        *cell |= msk;
        g_reached_count++;
    }
}

/**
 * CPUID. Reads the leaf from g_eax (and subleaf from g_ecx) and writes
 * g_eax/g_ebx/g_ecx/g_edx, modelling the Xbox's Pentium III.
 *
 * The lifter used to drop cpuid as a comment, which left all four registers
 * holding stale values - so the game's cached feature word was garbage and
 * SIMD dispatch was undefined. Implemented in recomp_manual.c.
 */
void recomp_cpuid(void);

/**
 * Probe-callable backtrace. Prints a tag, four caller-supplied values and the
 * real native call stack as RVAs, which triage_crash.py resolves against
 * build/*.map.
 *
 * Answers "who called me?" from anywhere, not just from a crash or a failed
 * indirect call. `limit` caps printing per tag; counting continues, and
 * recomp_where_summary() reports the true total at exit so a capped probe is
 * never mistaken for one that never fired.
 *
 * Returns the 1-based call number for this tag, so a probe can isolate the
 * first occurrence - usually the only one that explains anything.
 */
unsigned recomp_where(const char *tag, unsigned limit,
                      uint32_t a, uint32_t b, uint32_t c, uint32_t d);
void recomp_where_summary(void);

/* ================================================================
 * Callee-save checking (RECOMP_CHECK_ABI)
 * ================================================================ */

/**
 * ebx, esi and edi are callee-saved on x86. Here they are globals, so that
 * contract is carried entirely by the PUSH32/POP32 pairs in the generated
 * code - nothing in C enforces it, and a dropped pair is completely silent.
 * The caller simply continues with someone else's value.
 *
 * That is not theoretical. _initterm walks the CRT initialiser table with the
 * cursor in esi; a callee returned a stale esi, the cursor left the table, and
 * the loop began calling dwords straight off the stack - reaching
 * _except_handler3 in a boot where nothing had raised an exception.
 *
 * Build with -DRECOMP_CHECK_ABI to verify every direct call. Off by default:
 * the macro is emitted into all generated call sites either way, so switching
 * it on is a compile flag, not a regeneration.
 */
void recomp_abi_violation(const char *callee,
                          uint32_t ebx0, uint32_t esi0, uint32_t edi0);

/**
 * A call left esp outside the simulated stack.
 *
 * esp cannot be compared before and after like the other registers - it
 * legitimately moves by the callee's argument purge, and that amount is not
 * known at the call site. What IS knowable is that esp must still point into
 * the stack afterwards. Leaving it means some callee purged the wrong amount,
 * and reporting the first call where that becomes true names the culprit
 * directly instead of leaving a negative esp to surface as a wild write much
 * later.
 *
 * That is the shape of the very bug this checker was built for: an empty stub
 * purged nothing, and the damage appeared four frames up as a corrupted
 * register.
 */
void recomp_esp_escape(const char *callee, uint32_t esp_before);

/* Mark this textual call site as executed. See g_callsite_count above. */
/*
 * Software watchpoint poll. No-op unless the build defines RECOMP_WATCH_GUEST
 * and the run sets RECOMP_WATCH to an address. See src/kernel/xbox_watch.c:
 * the page-protection watchpoint cannot see writes that land while the page is
 * unprotected, so this reads the value after every call instead and names the
 * callee that changed it.
 */
#ifdef RECOMP_WATCH_GUEST
void xbox_WatchPoll(const char *site);
void xbox_WatchPollVA(uint32_t va);
#define RECOMP_WATCH_POLL(name) xbox_WatchPoll(name)
#define RECOMP_WATCH_POLL_VA(va)  xbox_WatchPollVA(va)
#else
#define RECOMP_WATCH_POLL(name) ((void)0)
#define RECOMP_WATCH_POLL_VA(va)  ((void)0)
#endif

#define RECOMP_MARK_SITE()                                           \
    do {                                                             \
        static uint8_t _site_seen;                                   \
        if (!_site_seen) { _site_seen = 1; g_callsite_count++; }     \
    } while (0)

#ifdef RECOMP_CHECK_ABI
/* Deliberately wider than the 8 MB stack: the point is to catch esp running
 * away entirely, not to police a few bytes either side of the guard page. */
#define RECOMP_ESP_LO 0x00700000u
#define RECOMP_ESP_HI 0x00F80000u
#define RECOMP_ABI_CALL(fn)                                          \
    do {                                                             \
        RECOMP_MARK_SITE();                                          \
        uint32_t _abi_b = g_ebx, _abi_s = g_esi, _abi_d = g_edi;     \
        uint32_t _abi_p = g_esp;                                     \
        fn();                                                        \
        if (g_ebx != _abi_b || g_esi != _abi_s || g_edi != _abi_d)   \
            recomp_abi_violation(#fn, _abi_b, _abi_s, _abi_d);       \
        if ((g_esp < RECOMP_ESP_LO || g_esp > RECOMP_ESP_HI) &&      \
            (_abi_p >= RECOMP_ESP_LO && _abi_p <= RECOMP_ESP_HI))    \
            recomp_esp_escape(#fn, _abi_p);                          \
    } while (0)
#else
#define RECOMP_ABI_CALL(fn) do { RECOMP_MARK_SITE(); RECOMP_WATCH_POLL("before " #fn); fn(); RECOMP_WATCH_POLL(#fn); } while (0)
#endif


/* ================================================================
 * Memory access helpers
 * ================================================================ */

/**
 * Translate an Xbox VA to an actual pointer.
 * Mask to 32-bit first: Xbox addresses are 32-bit and arithmetic
 * in the recompiled code can overflow. Without the mask, a 64-bit
 * uintptr_t cast preserves the overflow bits, landing us 4GB+ past
 * our mapping and causing access violations.
 */
#define XBOX_PTR(addr) ((uintptr_t)(uint32_t)(addr) + g_xbox_mem_offset)

/** Read/write N bytes at a flat Xbox memory address. */
#define MEM8(addr)   (*(volatile uint8_t  *)XBOX_PTR(addr))
#define MEM16(addr)  (*(volatile uint16_t *)XBOX_PTR(addr))
#define MEM32(addr)  (*(volatile uint32_t *)XBOX_PTR(addr))

/** Signed memory reads. */
#define SMEM8(addr)  (*(volatile int8_t   *)XBOX_PTR(addr))
#define SMEM16(addr) (*(volatile int16_t  *)XBOX_PTR(addr))
#define SMEM32(addr) (*(volatile int32_t  *)XBOX_PTR(addr))

/** Float/double memory access. */
#define MEMF(addr)   (*(volatile float    *)XBOX_PTR(addr))
#define MEMD(addr)   (*(volatile double   *)XBOX_PTR(addr))
/* 64-bit access, for MMX loads and stores. */
#define MEM64(addr)  (*(volatile uint64_t *)XBOX_PTR(addr))

/* Packed-integer helpers for the MMX instructions. */
#include "recomp_mmx.h"

/* ================================================================
 * Flag computation helpers
 *
 * These macros compute x86 flags for conditional branches.
 * Used by the lifter's pattern-matching output:
 *   cmp a, b; jcc target  ->  if (COND(a, b)) goto target;
 * ================================================================ */

/* Unsigned comparison conditions (from CMP a, b -> a - b) */
#define CMP_EQ(a, b)  ((uint32_t)(a) == (uint32_t)(b))
#define CMP_NE(a, b)  ((uint32_t)(a) != (uint32_t)(b))
#define CMP_B(a, b)   ((uint32_t)(a) <  (uint32_t)(b))   /* below (CF=1) */
#define CMP_AE(a, b)  ((uint32_t)(a) >= (uint32_t)(b))   /* above or equal */
#define CMP_BE(a, b)  ((uint32_t)(a) <= (uint32_t)(b))   /* below or equal */
#define CMP_A(a, b)   ((uint32_t)(a) >  (uint32_t)(b))   /* above */

/* Signed comparison conditions */
#define CMP_L(a, b)   ((int32_t)(a) <  (int32_t)(b))     /* less (SF!=OF) */
#define CMP_GE(a, b)  ((int32_t)(a) >= (int32_t)(b))     /* greater or equal */
#define CMP_LE(a, b)  ((int32_t)(a) <= (int32_t)(b))     /* less or equal */
#define CMP_G(a, b)   ((int32_t)(a) >  (int32_t)(b))     /* greater */

/* TEST-based conditions (AND without storing result) */
#define TEST_Z(a, b)  (((uint32_t)(a) & (uint32_t)(b)) == 0)  /* ZF=1 */
#define TEST_NZ(a, b) (((uint32_t)(a) & (uint32_t)(b)) != 0)  /* ZF=0 */
#define TEST_S(a, b)  ((int32_t)((uint32_t)(a) & (uint32_t)(b)) < 0) /* SF=1 */

/* ================================================================
 * Arithmetic with carry/overflow detection
 * ================================================================ */

/** Add with carry flag. Returns result, sets *cf. */
static inline uint32_t ADD32_CF(uint32_t a, uint32_t b, int *cf) {
    uint32_t r = a + b;
    *cf = (r < a);
    return r;
}

/** Sub with carry (borrow) flag. Returns result, sets *cf. */
static inline uint32_t SUB32_CF(uint32_t a, uint32_t b, int *cf) {
    *cf = (a < b);
    return a - b;
}

/* ================================================================
 * Rotation / shift helpers
 * ================================================================ */

static inline uint32_t ROL32(uint32_t val, int n) {
    n &= 31;
    return (val << n) | (val >> (32 - n));
}

static inline uint32_t ROR32(uint32_t val, int n) {
    n &= 31;
    return (val >> n) | (val << (32 - n));
}

/* ================================================================
 * Sign/zero extension
 * ================================================================ */

#define ZX8(v)   ((uint32_t)(uint8_t)(v))
#define ZX16(v)  ((uint32_t)(uint16_t)(v))
#define SX8(v)   ((uint32_t)(int32_t)(int8_t)(v))
#define SX16(v)  ((uint32_t)(int32_t)(int16_t)(v))

/* ================================================================
 * Byte/word register access
 *
 * These macros extract or set partial registers, matching x86
 * behavior where writing AL doesn't affect bits 8-31 of EAX.
 * ================================================================ */

/** Extract low byte (al, bl, cl, dl). */
#define LO8(r)  ((uint8_t)((r) & 0xFF))
/** Extract high byte of low word (ah, bh, ch, dh). */
#define HI8(r)  ((uint8_t)(((r) >> 8) & 0xFF))
/** Extract low word (ax, bx, cx, dx). */
#define LO16(r) ((uint16_t)((r) & 0xFFFF))

/** Set low byte, preserving upper 24 bits. */
#define SET_LO8(r, v)  ((r) = ((r) & 0xFFFFFF00u) | ((uint32_t)(uint8_t)(v)))
/** Set high byte of low word, preserving other bits. */
#define SET_HI8(r, v)  ((r) = ((r) & 0xFFFF00FFu) | (((uint32_t)(uint8_t)(v)) << 8))
/** Set low word, preserving upper 16 bits. */
#define SET_LO16(r, v) ((r) = ((r) & 0xFFFF0000u) | ((uint32_t)(uint16_t)(v)))

/* ================================================================
 * Stack simulation
 *
 * For push/pop heavy prologues in the generated code.
 * ================================================================ */

/**
 * Push a 32-bit value onto the simulated stack.
 * Evaluates val BEFORE decrementing sp, matching x86 semantics
 * where push [esp+N] reads the operand before adjusting ESP.
 */
#ifdef RECOMP_CHECK_ABI
/* Catch esp leaving the stack at the exact push that does it.
 *
 * RECOMP_ABI_CALL only sees call boundaries, so it cannot explain an esp that
 * runs away inside a function or across an indirect call - and that is exactly
 * where it was going wrong. This fires once, names the writing code through
 * the native backtrace, and then stays quiet so the run continues. */
#define PUSH32(sp, val) do { \
    uint32_t _pv = (uint32_t)(val); \
    (sp) -= 4; \
    if ((uint32_t)(sp) < RECOMP_ESP_LO || (uint32_t)(sp) > RECOMP_ESP_HI) \
        recomp_esp_escape("PUSH32", (uint32_t)(sp) + 4); \
    MEM32(sp) = _pv; \
} while(0)
#else
#define PUSH32(sp, val) do { \
    uint32_t _pv = (uint32_t)(val); \
    (sp) -= 4; \
    MEM32(sp) = _pv; \
} while(0)
#endif

/** Pop a 32-bit value from the simulated stack. */
#define POP32(sp, dst) do { \
    (dst) = MEM32(sp); \
    (sp) += 4; \
} while(0)

/* ================================================================
 * Byte swap (for endian conversion if needed)
 *
 * Xbox is little-endian like x86, so these are rarely needed,
 * but some games use bswap for network byte order or data parsing.
 * ================================================================ */

static inline uint32_t BSWAP32(uint32_t v) {
    return ((v >> 24) & 0xFF) | ((v >> 8) & 0xFF00) |
           ((v << 8) & 0xFF0000) | ((v << 24) & 0xFF000000u);
}

static inline uint16_t BSWAP16(uint16_t v) {
    return (uint16_t)((v >> 8) | (v << 8));
}

/* ================================================================
 * Indirect call dispatch
 *
 * The dispatch system resolves Xbox virtual addresses to native
 * function pointers at runtime. Three lookup sources are checked:
 *   1. Manual overrides (hand-written reimplementations)
 *   2. Generated dispatch table (auto-recompiled functions)
 *   3. Kernel thunk bridge (Xbox kernel function replacements)
 * ================================================================ */

/**
 * Generic function pointer type for all recompiled functions.
 * All translated functions are void(void) - arguments and return
 * values are passed through global registers and the simulated stack.
 */
#ifndef RECOMP_DISPATCH_H  /* avoid conflict with recomp_dispatch.h */
typedef void (*recomp_func_t)(void);

/**
 * Look up a recompiled function by its Xbox VA.
 * Returns NULL if the VA is not in the generated dispatch table.
 */
recomp_func_t recomp_lookup(uint32_t xbox_va);

/**
 * Look up a kernel thunk function by its synthetic VA.
 * Kernel thunks live at 0xFE000000+ (synthetic addresses assigned
 * during kernel bridge initialization).
 * Returns NULL if the VA is not a kernel thunk.
 */
recomp_func_t recomp_lookup_kernel(uint32_t xbox_va);

/**
 * Look up a manually overridden function by its Xbox VA.
 * Manual overrides take priority over generated code.
 * Returns NULL if no manual override exists for this VA.
 */
recomp_func_t recomp_lookup_manual(uint32_t xbox_va);
#endif

/*
 * RECOMP_ICALL_WATCH - ABI check around an INDIRECT call.
 *
 * RECOMP_CHECK_ABI's recomp_esp_escape() only wraps RECOMP_ABI_CALL, i.e.
 * direct calls. Running it for the first time (ledger #79) produced a useful
 * negative: 23 callee-save violations but ZERO esp reports, while the crash
 * itself has esp = 0x00F8031C - above the top of the stack, which starts at
 * 0x00F7FFF0 and grows down. So the over-pop is NOT in a direct call, and the
 * remaining suspects are indirect ones, which nothing was checking.
 *
 * This closes that gap. Unlike the direct-call version it can name the target
 * VA rather than a symbol, which is what you want here anyway: the likely
 * shape is an ICALL landing on a generic `g_esp += 4` stub whose real
 * epilogue owed more (ledger #71 found two such stubs owing 12 and 36 bytes,
 * and safe_stub still reads 8 every run).
 *
 * It also checks ebx/esi/edi, which RECOMP_ABI_CALL checks for direct calls
 * and nothing checked for indirect ones. That gap is not theoretical: the
 * page-zero census (docs/PAGE_ZERO_CENSUS.md) shows sub_00209650 null-checking
 * edi correctly ONCE, before its loop, then calling through a function pointer
 * each iteration and reading guest 0 on every later pass - edi is callee-saved
 * in the real x86 ABI, so the original code was entitled to assume it survived
 * that call. 19,390 reads of the null page come from that one register.
 *
 * Compiles to nothing without RECOMP_CHECK_ABI, so normal builds are
 * untouched.
 */
#ifdef RECOMP_CHECK_ABI
void recomp_esp_escape_va(uint32_t target_va, uint32_t esp_before);
void recomp_abi_violation_va(uint32_t target_va,
                             uint32_t ebx0, uint32_t esi0, uint32_t edi0);
#define RECOMP_ICALL_WATCH(va, call) do { \
    uint32_t _esp_b = g_esp; \
    uint32_t _abi_b = g_ebx, _abi_s = g_esi, _abi_d = g_edi; \
    call; \
    if ((g_esp < RECOMP_ESP_LO || g_esp > RECOMP_ESP_HI) && \
        (_esp_b >= RECOMP_ESP_LO && _esp_b <= RECOMP_ESP_HI)) \
        recomp_esp_escape_va((va), _esp_b); \
    if (g_ebx != _abi_b || g_esi != _abi_s || g_edi != _abi_d) \
        recomp_abi_violation_va((va), _abi_b, _abi_s, _abi_d); \
} while (0)
#else
#define RECOMP_ICALL_WATCH(va, call) do { call; } while (0)
#endif

/**
 * RECOMP_ICALL - Indirect call through the dispatch table.
 *
 * Looks up the Xbox VA and calls the translated function.
 * Falls back to kernel bridge for kernel thunk synthetic VAs.
 * The caller must PUSH32 a dummy return address before this macro.
 * If not found, pops the dummy return address to keep the stack balanced.
 *
 * The range check (0x00400000 to 0xFE000000) skips garbage VAs that
 * come from uninitialized vtable pointers. Adjust this range based
 * on your game's .text section boundaries. Kernel thunks at
 * 0xFE000000+ must NOT be blocked.
 *
 * CUSTOMIZE: Change the VA range check to match your game's code range.
 * Your .text section typically spans 0x00010000 to ~0x003XXXXX.
 * Any VA outside .text and below 0xFE000000 is likely garbage.
 */
#define RECOMP_ICALL(xbox_va) do { \
    uint32_t _va = (uint32_t)(xbox_va); \
    g_icall_trace[g_icall_trace_idx & (ICALL_TRACE_SIZE-1)] = _va; \
    g_icall_trace_idx++; \
    g_icall_count++; \
    /* Skip garbage VAs outside code section + kernel thunk range */ \
    if (_va >= 0x00400000 && _va < 0xFE000000) { \
        g_esp += 4; eax = 0; break; \
    } \
    recomp_func_t _fn = recomp_lookup_manual(_va); \
    if (!_fn) _fn = recomp_lookup(_va); \
    if (!_fn) _fn = recomp_lookup_kernel(_va); \
    if (_fn) { recomp_mark_reached(_va); RECOMP_WATCH_POLL("before icall"); RECOMP_ICALL_WATCH(_va, _fn()); RECOMP_WATCH_POLL_VA(_va); } \
    else { recomp_icall_fail_log(_va); g_esp += 4; eax = 0; } \
} while(0)

/**
 * RECOMP_ICALL_SAFE - Stack-safe indirect call.
 *
 * Restores g_esp to saved_esp (pre-argument value) on lookup failure,
 * preventing stdcall argument leaks on failed vtable calls.
 * Use this when the caller pushes arguments that the callee would
 * normally clean up (stdcall convention).
 */
#define RECOMP_ICALL_SAFE(xbox_va, saved_esp) do { \
    uint32_t _va = (uint32_t)(xbox_va); \
    g_icall_trace[g_icall_trace_idx & (ICALL_TRACE_SIZE-1)] = _va; \
    g_icall_trace_idx++; \
    g_icall_count++; \
    /* Reject known-bad addresses that are common corruption patterns: \
     * 0x00000000 (NULL), 0x00000004, 0x3F800000 (float 1.0), \
     * 0xFFFFFFFF (-1), 0xCCCCCCCC (uninit), 0xCDCDCDCD (freed), \
     * 0xFDFDFDFD (guard), 0xFEFEFEFE (guard). \
     * Also reject addresses below 0x00010000 (first 64KB) as they're \
     * never valid code pointers on Xbox. */ \
    if (_va < 0x00010000u || \
        _va == 0x00000004u || _va == 0x3F800000u || \
        _va == 0xFFFFFFFFu || _va == 0xCCCCCCCCu || \
        _va == 0xCDCDCDCDu || _va == 0xFDFDFDFDu || \
        _va == 0xFEFEFEFEu) { \
        recomp_icall_fail_log(_va); g_esp = (saved_esp); sub_00ICALL_SAFE_STUB(); break; \
    } \
    if (_va >= 0x00400000 && _va < 0xFE000000) { \
        recomp_icall_reject_log(_va, __FILE__, __LINE__); \
        g_esp = (saved_esp); sub_00ICALL_SAFE_STUB(); break; \
    } \
    recomp_func_t _fn = recomp_lookup_manual(_va); \
    if (!_fn) _fn = recomp_lookup(_va); \
    if (!_fn) _fn = recomp_lookup_kernel(_va); \
    if (_fn) { recomp_mark_reached(_va); RECOMP_WATCH_POLL("before icall"); RECOMP_ICALL_WATCH(_va, _fn()); RECOMP_WATCH_POLL_VA(_va); } \
    else { recomp_icall_fail_log(_va); g_esp = (saved_esp); sub_00ICALL_SAFE_STUB(); } \
} while(0)

/**
 * RECOMP_ITAIL - Indirect tail call (jmp through function pointer).
 *
 * No return address is pushed - reuses the current frame's return addr.
 * Used for tail-call optimization where the original code uses
 * jmp [reg] instead of call [reg].
 */
#define RECOMP_ITAIL(xbox_va) do { \
    uint32_t _tva = (uint32_t)(xbox_va); \
    recomp_func_t _fn = recomp_lookup_manual(_tva); \
    if (!_fn) _fn = recomp_lookup(_tva); \
    if (!_fn) _fn = recomp_lookup_kernel(_tva); \
    /* A tail call legitimately MOVES esp - the callee runs the epilogue and \
     * the ret for its caller - so no delta check is possible here. But esp \
     * must still never leave the stack range, which is all ESP_WATCH tests, \
     * so the check stays sound and covers the tail-jump chains that #79 \
     * could not rule out. */ \
    if (_fn) { recomp_mark_reached(_tva); RECOMP_ICALL_WATCH(_tva, _fn()); } \
    /* An unresolved tail jump used to do NOTHING here - no call, no log, no \
     * count. That is the worst possible failure for a tail jump, because the \
     * TARGET owns this function's epilogue: skipping it means the callee-saved \
     * registers are never popped and the caller silently receives garbage. \
     * Measured: sub_00342AA0 (memcpy) dispatches through a jump table whose \
     * entry sub_00342C8F is not lifted, and the ABI checker duly reported \
     * "sub_00342AA0 did not restore: esi ... edi ...". Route it through the \
     * same accounting every other unresolved indirect call already uses, so \
     * these show up in the failed-icall count instead of vanishing. */ \
    else { recomp_icall_fail_log(_tva); } \
} while(0)

/* ================================================================
 * Register name aliases for generated code
 *
 * Map x86 volatile register names to global variables.
 * These #defines allow the generated code to use natural register
 * names (eax, ecx, edx, esp) which the preprocessor maps to the
 * corresponding globals (g_eax, g_ecx, g_edx, g_esp).
 *
 * Only active when RECOMP_GENERATED_CODE is defined (in generated
 * .c files) to avoid polluting hand-written code.
 * ================================================================ */

#ifdef RECOMP_GENERATED_CODE
#define eax g_eax
#define ecx g_ecx
#define edx g_edx
#define esp g_esp
#define ebx g_ebx
#define esi g_esi
#define edi g_edi
/* ebp is NOT global - it's local in each function.
 * For __SEH_prolog/epilog, use g_seh_ebp to bridge. */
#endif

/* ================================================================
 * Forward declarations for translated functions
 *
 * These are generated by the recompiler and included per-file.
 * The recomp_funcs.h header (generated) declares all translated
 * function prototypes.
 * ================================================================ */

#endif /* RECOMP_TYPES_H */
