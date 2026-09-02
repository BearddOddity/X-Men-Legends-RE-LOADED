/*
 * d3d8_shim.c - native replacement for the Xbox D3D8 entry points.
 *
 * GENERATED SCAFFOLD (tools_data/gen_d3d8_shim.py), then hand-maintained.
 * Regenerating overwrites hand-written bodies - diff before you do.
 *
 * Why this file exists
 * ---------------------
 * Xbox D3D8 is statically linked into the XBE and called directly (not via
 * COM vtables), but the recomp pipeline never disassembles the D3D section.
 * Those entry points therefore fell through to recomp_stubs_unresolved.c as
 * genuinely empty functions, which broke two invariants:
 *
 *   1. Stack. The caller pushes a dummy return address and the callee must
 *      pop it plus its own __stdcall arguments. An empty stub popped
 *      nothing, leaking 4+N bytes of simulated stack on EVERY call.
 *   2. Return value. An empty stub never sets g_eax, so callers read a
 *      stale register - the source of many garbage-pointer crashes.
 *
 * Each function below performs the correct cleanup (esp += 4 + ret_imm,
 * matching the real `ret N` in the XBE) and returns an explicit value.
 *
 * PHASE 1 (current): correct calling convention + neutral return values.
 * This is what unblocks boot; nothing renders yet.
 * PHASE 2: implement real behaviour against a modern PC graphics backend.
 * This is a PC PORT, not Xbox emulation - do not reproduce NV2A pushbuffer
 * semantics here; translate to the backend's own state model instead.
 */

/*
 * NOTE: deliberately NOT defining RECOMP_GENERATED_CODE. That macro turns on
 * register aliasing (bare `eax` -> `g_eax`), which is convenient inside
 * mechanically generated code but hostile in hand-written C - it silently
 * captures any local named eax/ecx/esp. This file uses the g_-prefixed
 * globals explicitly, matching recomp_manual.c.
 */
#include "recomp_types.h"
#include "recomp/gen/recomp_funcs.h"  /* declarations, so signatures are checked */

/* Count of shim calls, for diagnostics. */
unsigned long g_d3d8_shim_calls = 0;

/*
 * D3D8_SHIM_TRACE - set to 1 to log every shim call, in order, with args.
 *
 * Which of these entry points actually run before a given crash is the
 * thing you need when deciding which to implement for real; guessing from
 * the API surface wastes effort on functions the game never reaches.
 * Arguments are logged too, because D3D8 Create* functions return an
 * HRESULT and hand the new object back through an out-pointer ARGUMENT
 * rather than in eax - so the argument values are what identify an
 * out-param that Phase 2 will need to fill in.
 */
#define D3D8_SHIM_TRACE 1

#include <windows.h>  /* for Sleep, HWND */

/*
 * From src/d3d (the xbox_d3d8 library). Declared here rather than including
 * d3d8_internal.h, which drags in the whole D3D11 surface for two symbols.
 *
 * d3d8_PresentFrame is safe before any device exists - it always pumps the
 * message queue and only presents when a swap chain has been created.
 * d3d8_GetHWND returns NULL until a device is created, which is how Swap
 * below decides whether anything is pacing the frame rate.
 */
void d3d8_PresentFrame(void);
HWND d3d8_GetHWND(void);
int  d3d8_CreateNativeDevice(unsigned width, unsigned height);

#if D3D8_SHIM_TRACE
#include <stdio.h>
/* Args live on the simulated stack: caller pushed them, then the dummy
 * return address. So arg1 is at g_esp + 4, arg2 at g_esp + 8, ... */
static void d3d8_trace(unsigned va, unsigned ret_imm)
{
    unsigned nargs = ret_imm / 4u;
    fprintf(stderr, "[D3D8] #%lu 0x%08X(", g_d3d8_shim_calls, va);
    for (unsigned i = 0; i < nargs && i < 8u; i++) {
        fprintf(stderr, "%s0x%08X", i ? ", " : "", MEM32(g_esp + 4u + i * 4u));
    }
    fprintf(stderr, ")\n");
    fflush(stderr);
}
#define D3D8_TRACE(va, n) d3d8_trace((va), (n))
#else
#define D3D8_TRACE(va, n) ((void)0)
#endif

/*
 * D3D8_SHIM_RET
 *
 * ret_imm is the callee's __stdcall argument cleanup, read from the real
 * `ret N` in the XBE. Total esp adjustment is 4 (dummy return address the
 * caller pushed) + ret_imm (the arguments). Trace BEFORE adjusting esp, so
 * the arguments are still addressable.
 */
#define D3D8_SHIM_RET(va, ret_imm, value)  \
    do { g_d3d8_shim_calls++;              \
         D3D8_TRACE((va), (ret_imm));      \
         g_eax = (value);                  \
         g_esp += 4 + (ret_imm); } while (0)

/* S_OK - most D3D8 calls that report status succeed trivially in Phase 1. */
#define D3D8_OK 0u

/* ================================================================
 * D3DResource reference counting
 *
 * AddRef and Release are the only two entries implemented for real rather
 * than stubbed, because a stub is not neutral here. Returning S_OK from
 * Release means no resource is ever freed and, worse, that any caller
 * branching on "did the count reach zero" takes the wrong branch. That is a
 * silent-wrong-answer failure of exactly the kind this project keeps paying
 * for, and between them the two are called from 38 sites.
 *
 * The arithmetic is transcribed from the original, not invented:
 *
 *   AddRef(pThis):
 *     if ((Common & 0xFFFF) == 0 && (Common & 0x70000) == 0x50000
 *         && *(pThis+0x14) != 0)  AddRef(*(pThis+0x14));
 *     Common += 1;  return Common & 0xFFFF;
 *
 *   Release(pThis):
 *     c = Common;
 *     if ((c & 0xFFFF) == 1) {
 *         if ((c & 0x70000) == 0x50000 && *(pThis+0x14) != 0)
 *             Release(*(pThis+0x14));
 *         c = Common;                       // re-read: the parent may change it
 *         if ((c & 0x780000) == 0) { DestroyResource(pThis); return 0; }
 *     }
 *     Common = c - 1;  return (c - 1) & 0xFFFF;
 *
 * Field encoding, read out of the same two functions:
 *   Common & 0x0000FFFF   external refcount
 *   Common & 0x00070000   type; 5 == surface (matches CreateImageSurface,
 *                         which builds a 24-byte header with Common 0x81050001)
 *   Common & 0x00780000   internal refcount - destruction is suppressed while
 *                         this is non-zero
 *   *(pThis + 0x14)       parent resource, AddRef'd/Released with a surface
 *
 * TWO DELIBERATE DEPARTURES, both consequences of the port being unfinished
 * rather than judgements about the game:
 *
 * 1. Destruction is counted, not performed. D3D_DestroyResource is neither
 *    shimmed nor available here, so there is nothing correct to call. Leaking
 *    is recoverable; freeing through a path that does not exist is not.
 *
 * 2. The pointer is range-checked before any write. The resource creators
 *    (CreateImageSurface and friends) are still stubs that never write their
 *    out-parameter, so the game can hand us a pointer the port itself failed
 *    to produce. Writing through it would manufacture a brand new defect and
 *    blame the game for it. The bound is [0x00880000, 0x04000000) - the same
 *    heap range the original binary uses for this judgement, already used by
 *    tools_data/add_guard.py and walls.py.
 *
 *    This guard is on OUR missing implementation, not on the game's data, so
 *    it is loud: every rejection is logged. It should start reporting zero
 *    once the creators are implemented, and if it does not, that is a finding.
 * ================================================================ */
#define D3D8_HEAP_LO 0x00880000u
#define D3D8_HEAP_HI 0x04000000u

#define D3D8_COMMON_REFCOUNT   0x0000FFFFu
#define D3D8_COMMON_TYPE       0x00070000u
#define D3D8_COMMON_TYPE_SURF  0x00050000u
#define D3D8_COMMON_INTREF     0x00780000u
#define D3D8_RESOURCE_PARENT   0x14u

unsigned g_d3d8_refcount_rejects;
unsigned g_d3d8_would_destroy;

/*
 * Turn a resource's Data field into a pointer the game can dereference.
 *
 * The original returns Data | 0x80000000 - the Xbox's cached alias of a
 * physical address, with Data holding ptr & 0x0FFFFFFF.
 *
 * DEPARTURE, forced by the port's memory model rather than by judgement:
 * xbox_memory_layout maps 64 MB of guest RAM at 0x00000000 and implements no
 * 0x80000000 alias, so a faithful return value would be unmapped and fault on
 * first use. Guest heap addresses are all below 0x10000000, so masking with
 * 0x0FFFFFFF is the identity here and the plain address is exactly the pointer
 * the game should write through.
 *
 * If a 0x80000000 alias is ever mapped, this becomes `| 0x80000000` and the
 * three Lock entries need no other change.
 */
static uint32_t d3d8_guest_ptr(uint32_t data)
{
    return data & 0x0FFFFFFFu;
}

/*
 * D3DResource_GetType, transcribed from 0x003613F0. Returns a D3DRESOURCETYPE,
 * which is NOT the raw Common type field - the mapping is not the identity.
 */
static uint32_t d3d8_ResourceGetType(uint32_t va)
{
    uint32_t t = MEM32(va) & 0x00070000u;
    uint32_t fmt3;
    if (t > 0x30000u) {
        fmt3 = MEM32(va + 0x0C);
        if (t == 0x40000u) {                                   /* texture */
            if (fmt3 & 4u) return 5u;
            return ((fmt3 & 0xF0u) > 0x20u) + 3u;
        }
        if (t != 0x50000u) return 10u;
        return ((fmt3 & 0xF0u) > 0x20u) + 1u;                  /* surface -> 1 */
    }
    if (t == 0x30000u) return 9u;                              /* palette */
    if (t != 0) return (t != 0x10000u) + 7u;
    return 6u;                                                 /* vertex buffer */
}

/* A pointer the port could plausibly have produced, holding something that
 * looks like a resource header. Type must be one of the seven D3D types. */
static int d3d8_resource_ok(uint32_t va)
{
    uint32_t common;
    if (va < D3D8_HEAP_LO || va >= D3D8_HEAP_HI || (va & 3u) != 0)
        return 0;
    common = MEM32(va);
    if (((common & D3D8_COMMON_TYPE) >> 16) > 6u)
        return 0;
    return 1;
}

static uint32_t d3d8_AddRef(uint32_t va, int depth)
{
    uint32_t common, parent;
    if (!d3d8_resource_ok(va)) {
        g_d3d8_refcount_rejects++;
        if (g_d3d8_refcount_rejects <= 8 || (g_d3d8_refcount_rejects % 1024) == 0)
            fprintf(stderr, "[D3D8] AddRef rejected implausible resource 0x%08X "
                            "(#%u) - creators are still stubs\n",
                    va, g_d3d8_refcount_rejects);
        return 0;
    }
    common = MEM32(va);
    if ((common & D3D8_COMMON_REFCOUNT) == 0 &&
        (common & D3D8_COMMON_TYPE) == D3D8_COMMON_TYPE_SURF && depth < 8) {
        parent = MEM32(va + D3D8_RESOURCE_PARENT);
        if (parent) d3d8_AddRef(parent, depth + 1);
    }
    common = MEM32(va) + 1;          /* re-read: the parent may share storage */
    MEM32(va) = common;
    return common & D3D8_COMMON_REFCOUNT;
}

static uint32_t d3d8_Release(uint32_t va, int depth)
{
    uint32_t common, parent;
    if (!d3d8_resource_ok(va)) {
        g_d3d8_refcount_rejects++;
        if (g_d3d8_refcount_rejects <= 8 || (g_d3d8_refcount_rejects % 1024) == 0)
            fprintf(stderr, "[D3D8] Release rejected implausible resource 0x%08X "
                            "(#%u) - creators are still stubs\n",
                    va, g_d3d8_refcount_rejects);
        return 0;
    }
    common = MEM32(va);
    if ((common & D3D8_COMMON_REFCOUNT) == 1) {
        if ((common & D3D8_COMMON_TYPE) == D3D8_COMMON_TYPE_SURF && depth < 8) {
            parent = MEM32(va + D3D8_RESOURCE_PARENT);
            if (parent) d3d8_Release(parent, depth + 1);
        }
        common = MEM32(va);          /* the original re-reads here too */
        if ((common & D3D8_COMMON_INTREF) == 0) {
            /* The original destroys here. See departure 1 above. */
            g_d3d8_would_destroy++;
            if (g_d3d8_would_destroy <= 8 || (g_d3d8_would_destroy % 256) == 0)
                fprintf(stderr, "[D3D8] resource 0x%08X reached refcount 0 "
                                "(type %u) - NOT destroyed, leaked on purpose "
                                "(#%u); no destroy path exists yet\n",
                        va, (common & D3D8_COMMON_TYPE) >> 16,
                        g_d3d8_would_destroy);
            MEM32(va) = common & ~D3D8_COMMON_REFCOUNT;   /* count reaches 0 */
            return 0;
        }
    }
    MEM32(va) = common - 1;
    return (common - 1) & D3D8_COMMON_REFCOUNT;
}

/* 0x0035ADD0  [D3D]  2 call site(s) */
void sub_0035ADD0(void)
{
    D3D8_SHIM_RET(0x0035ADD0u, 8, D3D8_OK);
}

/* 0x0035AE60  [D3D]  1 call site(s) */
void sub_0035AE60(void)
{
    D3D8_SHIM_RET(0x0035AE60u, 4, D3D8_OK);
}

/* 0x0035AE90  [D3D]  3 call site(s) */
void sub_0035AE90(void)
{
    D3D8_SHIM_RET(0x0035AE90u, 8, D3D8_OK);
}

/* 0x0035AFB0  [D3D]  1 call site(s) */
void sub_0035AFB0(void)
{
    D3D8_SHIM_RET(0x0035AFB0u, 4, D3D8_OK);
}

/* 0x0035B030  [D3D]  1 call site(s) */
void sub_0035B030(void)
{
    D3D8_SHIM_RET(0x0035B030u, 4, D3D8_OK);
}

/* 0x0035B040  [D3D]  1 call site(s) */
void sub_0035B040(void)
{
    D3D8_SHIM_RET(0x0035B040u, 0, D3D8_OK);
}

/* 0x0035B120  [D3D]  2 call site(s) */
void sub_0035B120(void)
{
    D3D8_SHIM_RET(0x0035B120u, 0, D3D8_OK);
}

/* 0x0035B340  [D3D]  2 call site(s) */
void sub_0035B340(void)
{
    D3D8_SHIM_RET(0x0035B340u, 4, D3D8_OK);
}

/* 0x0035B3F0  [D3D]  2 call site(s) */
void sub_0035B3F0(void)
{
    D3D8_SHIM_RET(0x0035B3F0u, 8, D3D8_OK);
}

/* 0x0035B670  [D3D]  2 call site(s) */
void sub_0035B670(void)
{
    D3D8_SHIM_RET(0x0035B670u, 4, D3D8_OK);
}

/* 0x0035B6C0  [D3D]  1 call site(s) */
void sub_0035B6C0(void)
{
    D3D8_SHIM_RET(0x0035B6C0u, 20, D3D8_OK);
}

/* 0x0035B9F0  [D3D]  1 call site(s) */
void sub_0035B9F0(void)
{
    D3D8_SHIM_RET(0x0035B9F0u, 0, D3D8_OK);
}

/* 0x0035BA10  [D3D]  1 call site(s) */
void sub_0035BA10(void)
{
    D3D8_SHIM_RET(0x0035BA10u, 4, D3D8_OK);
}

/* 0x0035BC40  [D3D]  9 call site(s) */
void sub_0035BC40(void)
{
    D3D8_SHIM_RET(0x0035BC40u, 8, D3D8_OK);
}

/* 0x0035BF00  [D3D]  3 call site(s) */
void sub_0035BF00(void)
{
    D3D8_SHIM_RET(0x0035BF00u, 8, D3D8_OK);
}

/* 0x0035C060  [D3D]  7 call site(s) */
void sub_0035C060(void)
{
    D3D8_SHIM_RET(0x0035C060u, 8, D3D8_OK);
}

/* 0x0035C210  [D3D]  2 call site(s) */
void sub_0035C210(void)
{
    D3D8_SHIM_RET(0x0035C210u, 8, D3D8_OK);
}

/* 0x0035C2A0  [D3D]  1 call site(s) */
void sub_0035C2A0(void)
{
    D3D8_SHIM_RET(0x0035C2A0u, 8, D3D8_OK);
}

/* 0x0035CFC0  [D3D]  1 call site(s) */
void sub_0035CFC0(void)
{
    D3D8_SHIM_RET(0x0035CFC0u, 16, D3D8_OK);
}

/* 0x0035D100  [D3D]  1 call site(s) */
void sub_0035D100(void)
{
    D3D8_SHIM_RET(0x0035D100u, 0, D3D8_OK);
}

/* 0x0035D160  [D3D]  1 call site(s) */
void sub_0035D160(void)
{
    D3D8_SHIM_RET(0x0035D160u, 0, D3D8_OK);
}

/* 0x0035D330  [D3D]  1 call site(s) */
void sub_0035D330(void)
{
    D3D8_SHIM_RET(0x0035D330u, 12, D3D8_OK);
}

/* 0x0035D360  [D3D]  4 call site(s) */
void sub_0035D360(void)
{
    D3D8_SHIM_RET(0x0035D360u, 12, D3D8_OK);
}

/* 0x0035D650  [D3D]  2 call site(s) */
void sub_0035D650(void)
{
    D3D8_SHIM_RET(0x0035D650u, 4, D3D8_OK);
}

/* 0x0035D760  [D3D]  3 call site(s) */
void sub_0035D760(void)
{
    D3D8_SHIM_RET(0x0035D760u, 4, D3D8_OK);
}

/*
 * 0x0035D900  [D3D]  36 call site(s)
 *
 * D3DDevice_SetRenderState_Simple(DWORD Method, DWORD Value)  __fastcall
 *
 * This was previously implemented as Present, with a ~60 FPS frame limiter and
 * a "Present called N times" counter. It is not Present. Corrected 2026-09-02
 * on three pieces of evidence:
 *
 *  - Its own bytes. It appends two dwords to the XDK push buffer, nothing else:
 *        mov eax,[0036CB00]   ; push pointer
 *        add eax,8            ; append two dwords
 *        cmp eax,[0036CB04]   ; limit
 *        jae slow_path        ; flush / grow
 *        mov [0036CB00],eax
 *        mov [eax-8],ecx      ; Method
 *        mov [eax-4],edx      ; Value
 *        ret
 *  - The real present entry exists separately - D3DDevice_Swap at 0x00368BE0,
 *    which is where the frame pump now lives.
 *  - 36 distinct call sites suits a state setter called from everywhere. A
 *    game presents from one place.
 *
 * The limiter mattered: it slept up to 16 ms on every render-state change,
 * which is the most-called graphics entry in the binary.
 *
 * Arguments arrive in ecx/edx, not on the stack, so ret_imm stays 0.
 *
 * Still a stub as far as rendering goes. Translating Method/Value onto the
 * native state machine in src/d3d/d3d8_states.c is the actual porting work;
 * the trace is here so that state stream is visible in the meantime.
 */
void sub_0035D900(void)
{
    static unsigned n;
    if (++n <= 8 || (n % 4096) == 0) {
        fprintf(stderr, "[D3D8] SetRenderState_Simple #%u method=0x%08X value=0x%08X\n",
                n, g_ecx, g_edx);
        fflush(stderr);
    }
    D3D8_SHIM_RET(0x0035D900u, 0, D3D8_OK);
}

/* 0x0035DC40  [D3D]  1 call site(s) */
void sub_0035DC40(void)
{
    D3D8_SHIM_RET(0x0035DC40u, 4, D3D8_OK);
}

/* 0x0035DC80  [D3D]  1 call site(s) */
void sub_0035DC80(void)
{
    D3D8_SHIM_RET(0x0035DC80u, 4, D3D8_OK);
}

/* 0x0035DCD0  [D3D]  3 call site(s) */
void sub_0035DCD0(void)
{
    D3D8_SHIM_RET(0x0035DCD0u, 4, D3D8_OK);
}

/* 0x0035DD80  [D3D]  2 call site(s) */
void sub_0035DD80(void)
{
    D3D8_SHIM_RET(0x0035DD80u, 4, D3D8_OK);
}

/* 0x0035DDC0  [D3D]  1 call site(s) */
void sub_0035DDC0(void)
{
    D3D8_SHIM_RET(0x0035DDC0u, 4, D3D8_OK);
}

/* 0x0035DE20  [D3D]  3 call site(s) */
void sub_0035DE20(void)
{
    D3D8_SHIM_RET(0x0035DE20u, 4, D3D8_OK);
}

/* 0x0035DF10  [D3D]  1 call site(s) */
void sub_0035DF10(void)
{
    D3D8_SHIM_RET(0x0035DF10u, 4, D3D8_OK);
}

/* 0x0035DFF0  [D3D]  2 call site(s) */
void sub_0035DFF0(void)
{
    D3D8_SHIM_RET(0x0035DFF0u, 4, D3D8_OK);
}

/* 0x0035E170  [D3D]  3 call site(s) */
void sub_0035E170(void)
{
    D3D8_SHIM_RET(0x0035E170u, 8, D3D8_OK);
}

/* 0x0035E280  [D3D]  10 call site(s) */
void sub_0035E280(void)
{
    D3D8_SHIM_RET(0x0035E280u, 12, D3D8_OK);
}

/* 0x0035E2F0  [D3D]  1 call site(s) */
void sub_0035E2F0(void)
{
    D3D8_SHIM_RET(0x0035E2F0u, 8, D3D8_OK);
}

/* 0x0035E330  [D3D]  1 call site(s) */
void sub_0035E330(void)
{
    D3D8_SHIM_RET(0x0035E330u, 8, D3D8_OK);
}

/* 0x0035EB50  [D3D]  2 call site(s) */
void sub_0035EB50(void)
{
    D3D8_SHIM_RET(0x0035EB50u, 4, D3D8_OK);
}

/* 0x0035EBE0  [D3D]  2 call site(s) */
void sub_0035EBE0(void)
{
    D3D8_SHIM_RET(0x0035EBE0u, 4, D3D8_OK);
}

/* 0x0035EC80  [D3D]  2 call site(s) */
void sub_0035EC80(void)
{
    D3D8_SHIM_RET(0x0035EC80u, 4, D3D8_OK);
}

/* 0x00361360  [D3D]  3 call site(s) */
/* ================================================================
 * Resource creation
 *
 * These allocate real objects in GUEST memory, because that is what the game
 * stores and later hands back to AddRef, Release, GetDesc and Lock. A host
 * allocation would put a host pointer inside a guest structure and break the
 * moment anything dereferenced it.
 *
 * Guest allocation goes through the game's own XMemAlloc at 0x001A1DDB, which
 * lives in .text and so is lifted and callable. Its convention is settled from
 * the call site in CreateVertexBuffer2:
 *
 *     PUSH 0x64800000 ; PUSH 0xc ; CALL 0x001a1ddb ; MOV ESI,EAX
 *
 * with no stack cleanup afterwards - __stdcall, arguments right to left, so
 * arg1 = size, arg2 = attributes, and the callee pops.
 * ================================================================ */
extern void sub_001A1DDB(void);   /* XMemAlloc(size, attrs) __stdcall */
extern void sub_001A1E7B(void);   /* XMemFree(ptr, attrs)   __stdcall */

/*
 * Payload arena (src/kernel/xbox_memory_layout.c). Guest-addressable storage
 * OUTSIDE the emulated Xbox heap, so a texture no longer has to fit in a 2001
 * memory budget just because the console had no choice.
 *
 * Returns 0 when the arena is disabled (the default) or full, and every caller
 * below falls back to the game's own XMemAlloc - so behaviour is unchanged
 * unless XBOX_PAYLOAD_MB is set.
 *
 * Only the payload moves. The resource HEADER stays in Xbox RAM because the
 * game inspects it, and because `Common`, `Data` and the packed size words are
 * what AddRef, Release, GetDesc and LockRect all read.
 */
extern uint32_t xbox_PayloadAlloc(uint32_t size, uint32_t align);

unsigned g_d3d8_payload_bytes;
unsigned g_d3d8_payload_allocs;

/*
 * Allocate resource payload storage: arena first, guest heap second.
 *
 * The arena sits at 0x0C000000..0x10000000, so `& 0x0FFFFFFF` - which the XDK
 * applies when storing a resource Data pointer - is still the identity on
 * these addresses. That is exactly why the arena has to end at 256 MB.
 */
static uint32_t d3d8_PayloadAlloc(uint32_t size, uint32_t attrs)
{
    uint32_t va = xbox_PayloadAlloc(size, 64u);   /* 64-byte, matches D3D pitch */
    if (va) {
        g_d3d8_payload_allocs++;
        g_d3d8_payload_bytes += size;
        return va;
    }
    return d3d8_XMemAlloc(size, attrs);
}

static uint32_t d3d8_XMemAlloc(uint32_t size, uint32_t attrs)
{
    uint32_t saved = g_esp;
    g_esp -= 4; MEM32(g_esp) = attrs;
    g_esp -= 4; MEM32(g_esp) = size;
    g_esp -= 4; MEM32(g_esp) = 0;          /* dummy return address */
    RECOMP_ABI_CALL(sub_001A1DDB);
    /* __stdcall pops its own arguments; restore explicitly anyway, the same
     * way D3D8_SHIM_RET fixes esp by hand rather than trusting the callee. */
    g_esp = saved;
    return g_eax;
}

static void d3d8_XMemFree(uint32_t ptr, uint32_t attrs)
{
    uint32_t saved = g_esp;
    g_esp -= 4; MEM32(g_esp) = attrs;
    g_esp -= 4; MEM32(g_esp) = ptr;
    g_esp -= 4; MEM32(g_esp) = 0;
    RECOMP_ABI_CALL(sub_001A1E7B);
    g_esp = saved;
}

/*
 * Surface size and the packed Format/Size header words, transcribed from
 * D3D8's own helper at 0x00361EF0.
 *
 * It indexes a per-format attribute table at 0x0036A270. That table is in the
 * D3D block, which the port replaces as *code* but still maps as *data*, so
 * the original table is read here rather than re-derived. Bits 2..5 of the
 * entry are bits-per-pixel; bit 0 marks a swizzled format.
 *
 * ONLY THE LINEAR PATH IS IMPLEMENTED. The swizzled path in the original walks
 * a mip chain using a log2 helper at 0x00361720 whose exact rounding is not
 * established, and guessing it would produce a plausible-looking wrong
 * allocation size - the kind of defect that surfaces much later as heap
 * corruption. A swizzled request fails loudly instead. Both of the game's
 * CreateImageSurface call sites are expected to be linear; if this fires, the
 * log says so and the helper needs transcribing properly.
 */
/*
 * Count trailing zeros, transcribed from 0x00361720. For the power-of-two
 * dimensions a swizzled format requires this is log2; it returns 0 for 0,
 * exactly as the original does.
 */
static uint32_t d3d8_ctz(uint32_t v)
{
    uint32_t i = 0;
    if (v == 0) return 0;
    while (((v >> i) & 1u) == 0) i++;
    return i;
}

#define D3D8_FORMAT_ATTR_TABLE 0x0036A270u
#define D3D8_PALETTE_SIZE_TABLE 0x0036A93Cu

unsigned g_d3d8_swizzled_rejects;

static int d3d8_surface_layout(uint32_t width, uint32_t height, uint32_t format,
                               uint32_t depth, uint32_t levels, uint32_t cubemap,
                               uint32_t *out_size, uint32_t *out_fmt_word,
                               uint32_t *out_size_word)
{
    uint32_t attr, bpp_bits, pitch;

    attr = MEM8(D3D8_FORMAT_ATTR_TABLE + format);
    bpp_bits = attr & 0x3Cu;

    /* The original's own test for "needs the swizzled path". */
    if ((attr & 1u) != 0 || format == 0xC || (format >= 0xE && format <= 0xF)) {
        /*
         * Swizzled path, transcribed from the same helper. Dimensions become
         * log2 exponents and the size is the sum over the mip chain; the size
         * word is left at 0, which is how the decoder at 0x00361A10 knows to
         * read dimensions back out of the format word instead.
         *
         * Depth is 1 and level count 1 for every call this shim makes, but the
         * general form is kept so the one caller that passes something else
         * does not silently get the wrong answer.
         */
        uint32_t w_log2 = d3d8_ctz(width);
        uint32_t h_log2 = d3d8_ctz(height);
        uint32_t d_log2 = d3d8_ctz(depth);
        uint32_t clamp  = (format == 0xC || (format >= 0xE && format <= 0xF)) ? 2u : 0u;
        uint32_t ws = w_log2, hs = h_log2, ds = d_log2;
        uint32_t total = 0, n;

        if (levels == 0) {
            uint32_t m = h_log2 > d_log2 ? h_log2 : d_log2;
            levels = (w_log2 > m ? w_log2 : m) + 1u;
        }
        for (n = levels; n != 0; n--) {
            uint32_t a = ws > clamp ? ws : clamp;
            uint32_t b = hs > clamp ? hs : clamp;
            total += ((1u << ((a + b + ds) & 0x1Fu)) * bpp_bits) >> 3;
            if (ws) ws--;
            if (hs) hs--;
            if (ds) ds--;
        }
        if (cubemap) total = ((total + 0x7Fu) & 0xFFFFFF80u) * 6u;

        *out_size = total;
        *out_fmt_word =
            (((((d_log2 << 4 | h_log2) << 4 | w_log2) << 4 | levels) << 8 | format) << 4
             | 2u) << 4 | (cubemap ? 4u : 0u) | 9u;
        *out_size_word = 0;      /* swizzled: the decoder uses the format word */
        return 1;
    }

    /* pitch = ((bpp_bits * width) >> 3), rounded up to 64 bytes. */
    pitch = ((bpp_bits * width) >> 3) + 0x3Fu;
    pitch &= 0xFFFFFFC0u;
    *out_size = height * pitch;

    /* Packed format word, exactly as the original assembles it: depth, height
     * and width log2 fields are zero on the linear path, mip count 1. */
    *out_fmt_word = (((((0u << 4 | 0u) << 4 | 0u) << 4 | (levels ? levels : 1u))
                      << 8 | format) << 4 | 2u) << 4 | 9u;

    /* Packed size word: pitch in 64-byte units, then height-1 and width-1. */
    *out_size_word = ((((pitch >> 6) - 1u) * 0x1000u) | (height - 1u)) << 12 | (width - 1u);
    return 1;
}

/*
 * D3DResource_AddRef(D3DResource *pThis) -> ULONG new refcount
 *
 * Implemented, not stubbed. See the reference-counting block near the top.
 * One stack argument, so pThis is at g_esp + 4 (the caller pushed it, then
 * the dummy return address).
 */
void sub_00361360(void)
{
    uint32_t pThis = MEM32(g_esp + 4);
    uint32_t rc = d3d8_AddRef(pThis, 0);
    D3D8_SHIM_RET(0x00361360u, 4, rc);
}

/*
 * D3DResource_Release(D3DResource *pThis) -> ULONG remaining refcount
 *
 * The busiest resource entry in the binary at 35 call sites. Returning S_OK
 * unconditionally, as this used to, meant every caller saw "0 references
 * remaining" and nothing was ever released or reused correctly.
 */
void sub_003613A0(void)
{
    uint32_t pThis = MEM32(g_esp + 4);
    uint32_t rc = d3d8_Release(pThis, 0);
    D3D8_SHIM_RET(0x003613A0u, 4, rc);
}

/* 0x00361560  [D3D]  2 call site(s) */
/*
 * D3DDevice_CreateVertexBuffer2(UINT Length) -> D3DVertexBuffer *
 *
 * Transcribed from the original at 0x00361560. Returns the object in eax; the
 * stub used to return 0, so every vertex buffer the game made was NULL.
 *
 *   header = XMemAlloc(0x0C, 0x64800000)     Common, Data, Lock
 *   data   = XMemAlloc(Length, 0xB2800000)
 *   Common = 0x01000001    type 0 (vertex buffer), refcount 1, D3DCREATED
 *   Data   = data & 0x0FFFFFFF
 */
void sub_00361560(void)
{
    uint32_t length = MEM32(g_esp + 4);
    uint32_t hdr = d3d8_XMemAlloc(0x0Cu, 0x64800000u);
    uint32_t data;
    if (hdr) {
        data = d3d8_PayloadAlloc(length, 0xB2800000u);   /* vertices: arena first */
        if (data) {
            MEM32(hdr + 4) = data & 0x0FFFFFFFu;
            MEM32(hdr + 0) = 0x01000001u;
            MEM32(hdr + 8) = 0;
            D3D8_SHIM_RET(0x00361560u, 4, hdr);
            return;
        }
        d3d8_XMemFree(hdr, 0x24800000u);
    }
    D3D8_SHIM_RET(0x00361560u, 4, 0u);
}

/* 0x003615B0  [D3D]  2 call site(s) */
/*
 * D3DVertexBuffer_Lock2(D3DVertexBuffer *pThis, DWORD Flags) -> BYTE *
 *
 * Transcribed from 0x003615B0. The original also appends a 0x41710 command to
 * the push buffer unless Flags & 0x10, and waits on the resource unless
 * Flags & 0xA0. Neither is reproduced: this is a PC port with no push buffer
 * and nothing renders asynchronously, so there is nothing to synchronise with.
 * The return value is the part the game uses.
 */
void sub_003615B0(void)
{
    uint32_t pThis = MEM32(g_esp + 4);
    uint32_t ptr = 0;
    if (d3d8_resource_ok(pThis)) {
        ptr = d3d8_guest_ptr(MEM32(pThis + 4));
    } else {
        g_d3d8_refcount_rejects++;
        fprintf(stderr, "[D3D8] VertexBuffer_Lock2 rejected 0x%08X\n", pThis);
        fflush(stderr);
    }
    D3D8_SHIM_RET(0x003615B0u, 8, ptr);
}

/* 0x00361600  [D3D]  1 call site(s) */
/*
 * D3DDevice_CreatePalette2(D3DPALETTESIZE Size) -> D3DPalette *
 *
 * Transcribed from the original at 0x00361600. The entry count comes from the
 * table at 0x0036A93C, read out of the mapped image rather than re-derived.
 *
 *   Common = (Size << 30) | 0x01030001    type 3 (palette), refcount 1
 */
void sub_00361600(void)
{
    uint32_t size = MEM32(g_esp + 4);
    uint32_t bytes = MEM32(D3D8_PALETTE_SIZE_TABLE + size * 4u);
    uint32_t hdr = d3d8_XMemAlloc(0x0Cu, 0x64800000u);
    uint32_t data;
    if (hdr) {
        data = d3d8_PayloadAlloc(bytes, 0xB6800000u);   /* palette: arena first */
        if (data) {
            MEM32(hdr + 0) = (size << 30) | 0x01030001u;
            MEM32(hdr + 4) = data & 0x0FFFFFFFu;
            MEM32(hdr + 8) = 0;
            D3D8_SHIM_RET(0x00361600u, 4, hdr);
            return;
        }
        d3d8_XMemFree(hdr, 0x24800000u);
    }
    D3D8_SHIM_RET(0x00361600u, 4, 0u);
}

/* 0x00361660  [D3D]  1 call site(s) */
/*
 * D3DPalette_Lock2(D3DPalette *pThis, DWORD Flags) -> D3DCOLOR *
 *
 * Transcribed from 0x00361660. Same shape as the vertex buffer lock, minus the
 * push-buffer command: the original only waits on the resource unless
 * Flags & 0xA0, which this port has nothing to wait for.
 */
void sub_00361660(void)
{
    uint32_t pThis = MEM32(g_esp + 4);
    uint32_t ptr = 0;
    if (d3d8_resource_ok(pThis)) {
        ptr = d3d8_guest_ptr(MEM32(pThis + 4));
    } else {
        g_d3d8_refcount_rejects++;
        fprintf(stderr, "[D3D8] Palette_Lock2 rejected 0x%08X\n", pThis);
        fflush(stderr);
    }
    D3D8_SHIM_RET(0x00361660u, 8, ptr);
}

/* 0x00362430  [D3D]  1 call site(s) */
void sub_00362430(void)
{
    D3D8_SHIM_RET(0x00362430u, 24, D3D8_OK);
}

/* 0x003658E0  [D3D]  2 call site(s) */
/*
 * D3DDevice_CreateImageSurface(Width, Height, <unused>, Format) -> D3DSurface *
 *
 * Transcribed from the original at 0x003658E0. The third argument is accepted
 * and ignored, which is what the original does with it too.
 *
 *   header = XMemAlloc(0x18, 0x64800000)   Common, Data, Lock, Format, Size, Parent
 *   data   = XMemAlloc(size, 0xB6800000)
 *   Common = 0x81050001    type 5 (surface), refcount 1, D3DCREATED
 *   +0x0C  = packed format word   from the layout helper
 *   +0x10  = packed size word     from the layout helper
 *   +0x14  = 0                    no parent - this surface owns itself
 *
 * The refcount of 1 is why AddRef/Release had to be real first: a surface
 * arrives here already holding one reference, and the game's Release is what
 * is supposed to take it away.
 */
void sub_003658E0(void)
{
    uint32_t width  = MEM32(g_esp + 4);
    uint32_t height = MEM32(g_esp + 8);
    uint32_t format = MEM32(g_esp + 16);
    uint32_t size = 0, fmt_word = 0, size_word = 0, hdr, data;

    if (!d3d8_surface_layout(width, height, format, 1u, 1u, 0u,
                             &size, &fmt_word, &size_word)) {
        D3D8_SHIM_RET(0x003658E0u, 16, 0u);
        return;
    }

    hdr = d3d8_XMemAlloc(0x18u, 0x64800000u);
    if (hdr) {
        data = d3d8_PayloadAlloc(size, 0xB6800000u);   /* pixels: arena first */
        if (data) {
            MEM32(hdr + 0x04) = data & 0x0FFFFFFFu;
            MEM32(hdr + 0x00) = 0x81050001u;
            MEM32(hdr + 0x0C) = fmt_word;
            MEM32(hdr + 0x10) = size_word;
            MEM32(hdr + 0x14) = 0;
            MEM32(hdr + 0x08) = 0;
            fprintf(stderr, "[D3D8] CreateImageSurface %ux%u fmt=0x%X -> 0x%08X "
                            "(%u bytes)\n", width, height, format, hdr, size);
            fflush(stderr);
            D3D8_SHIM_RET(0x003658E0u, 16, hdr);
            return;
        }
        d3d8_XMemFree(hdr, 0x24800000u);
    }
    D3D8_SHIM_RET(0x003658E0u, 16, 0u);
}

/* 0x00365970  [D3D]  5 call site(s) */
/*
 * D3DSurface_GetDesc(D3DSurface *pThis, D3DSURFACE_DESC *pDesc)
 *
 * The original forwards to Get2DSurfaceDesc(pThis, 0, pDesc) at 0x00361B60,
 * which is transcribed here along with its size decoder at 0x00361A10.
 *
 * That decoder independently confirms the encoding CreateImageSurface writes:
 * it reads width as (word & 0xFFF)+1, height as ((word>>12) & 0xFFF)+1 and
 * pitch as ((word>>24)+1)*64 - exactly the packing used there - and takes the
 * format from the byte at +0x0D. Encoder and decoder were transcribed from
 * different functions and agree, which is the check on both.
 *
 * D3DSURFACE_DESC: [0] Format [1] Type [2] Usage [3] Size
 *                  [4] MultiSampleType [5] Width [6] Height
 *
 * One field is device dependent. The original sets MultiSampleType from device
 * state when the surface IS the current render target, and 0x11 otherwise.
 * The port creates no device object, so no surface can be the render target
 * and 0x11 is the branch the original itself takes - this is the faithful
 * answer here, not a placeholder.
 */
void sub_00365970(void)
{
    uint32_t pThis = MEM32(g_esp + 4);
    uint32_t pDesc = MEM32(g_esp + 8);
    uint32_t common, size_word, attr, fmt;
    uint32_t width = 0, height = 0, pitch = 0, size = 0, type, usage = 0;

    if (!d3d8_resource_ok(pThis) || pDesc < D3D8_HEAP_LO) {
        g_d3d8_refcount_rejects++;
        fprintf(stderr, "[D3D8] GetDesc rejected surface 0x%08X desc 0x%08X\n",
                pThis, pDesc);
        fflush(stderr);
        D3D8_SHIM_RET(0x00365970u, 8, D3D8_OK);
        return;
    }

    common    = MEM32(pThis + 0x00);
    fmt       = MEM8(pThis + 0x0D);
    size_word = MEM32(pThis + 0x10);
    attr      = MEM8(D3D8_FORMAT_ATTR_TABLE + fmt);

    /* Usage, from the same attribute table the original consults. */
    if (attr & 0x80u)      usage = 1;   /* render target */
    else if (attr & 0x40u) usage = 2;   /* depth stencil */

    type = d3d8_ResourceGetType(pThis);

    if (size_word != 0) {               /* linear - the path we create */
        width  = (size_word & 0xFFFu) + 1u;
        height = ((size_word >> 12) & 0xFFFu) + 1u;
        pitch  = ((size_word >> 24) + 1u) * 0x40u;
        size   = height * pitch;
    } else {
        /*
         * Swizzled: the size word is 0 and the dimensions live in the format
         * word instead. Transcribed from the same decoder's other branch.
         * Level 0 is the only level GetDesc asks for, so the "- level" terms
         * the original applies are all zero here.
         *
         *   bits 20-23 width log2, 24-27 height log2, 28-31 depth log2
         *
         * which is precisely where the encoder above places them.
         */
        uint32_t fw = MEM32(pThis + 0x0C);
        uint32_t w_log2 = (fw >> 20) & 0xFu;
        uint32_t h_log2 = (fw >> 24) & 0xFu;
        uint32_t clamp  = (fmt == 0xC || (fmt > 0xD && fmt < 0x10)) ? 2u : 0u;
        uint32_t a = w_log2 > clamp ? w_log2 : clamp;
        uint32_t b = h_log2 > clamp ? h_log2 : clamp;
        uint32_t iv = 1u << (a & 0x1Fu);

        width  = w_log2 < 1u ? 1u : (1u << (w_log2 & 0x1Fu));
        height = h_log2 < 1u ? 1u : (1u << (h_log2 & 0x1Fu));

        if (fmt == 0xC)                    pitch = iv * 2u;
        else if (fmt < 0xE || fmt > 0xF)   pitch = (iv * (attr & 0x3Cu)) >> 3;
        else                               pitch = iv * 4u;

        size = ((1u << (b & 0x1Fu)) * iv * (attr & 0x3Cu)) >> 3;
    }

    MEM32(pDesc + 0x00) = fmt;
    MEM32(pDesc + 0x04) = type;
    MEM32(pDesc + 0x08) = usage;
    MEM32(pDesc + 0x0C) = size;
    MEM32(pDesc + 0x10) = 0x11u;        /* not the render target - see above */
    MEM32(pDesc + 0x14) = width;
    MEM32(pDesc + 0x18) = height;

    (void)common;
    D3D8_SHIM_RET(0x00365970u, 8, D3D8_OK);
}

/*
 * D3DSurface_LockRect(D3DSurface *pThis, D3DLOCKED_RECT *pLocked,
 *                     const RECT *pRect, DWORD Flags)
 *
 * The original forwards to Lock2DSurface(pThis, face 0, level 0, ...).
 * D3DLOCKED_RECT is { INT Pitch; void *pBits; }.
 *
 * The whole-surface case (pRect == NULL) is fully determined: pitch and the
 * base pointer both come from fields this shim writes and decodes.
 *
 * A sub-rectangle lock is NOT transcribed - Lock2DSurface's exact handling of
 * rect offsets and swizzled layouts was not established. The standard
 * top*pitch + left*bpp offset is applied and the call is logged, so if the
 * game ever takes that path it is visible rather than silently assumed
 * correct.
 */
void sub_00365990(void)
{
    uint32_t pThis  = MEM32(g_esp + 4);
    uint32_t pLock  = MEM32(g_esp + 8);
    uint32_t pRect  = MEM32(g_esp + 12);
    uint32_t flags  = MEM32(g_esp + 16);
    uint32_t size_word, data, pitch, bits, fmt, bpp;

    if (!d3d8_resource_ok(pThis) || pLock < D3D8_HEAP_LO) {
        g_d3d8_refcount_rejects++;
        fprintf(stderr, "[D3D8] LockRect rejected surface 0x%08X locked 0x%08X\n",
                pThis, pLock);
        fflush(stderr);
        D3D8_SHIM_RET(0x00365990u, 16, D3D8_OK);
        return;
    }

    size_word = MEM32(pThis + 0x10);
    data      = MEM32(pThis + 0x04);
    fmt       = MEM8(pThis + 0x0D);
    bits      = d3d8_guest_ptr(data);
    if (size_word) {
        pitch = ((size_word >> 24) + 1u) * 0x40u;
    } else {
        /* Swizzled - same derivation the decoder uses, see GetDesc. */
        uint32_t fw = MEM32(pThis + 0x0C);
        uint32_t w_log2 = (fw >> 20) & 0xFu;
        uint32_t attr2  = MEM8(D3D8_FORMAT_ATTR_TABLE + fmt);
        uint32_t clamp  = (fmt == 0xC || (fmt > 0xD && fmt < 0x10)) ? 2u : 0u;
        uint32_t a  = w_log2 > clamp ? w_log2 : clamp;
        uint32_t iv = 1u << (a & 0x1Fu);
        if (fmt == 0xC)                  pitch = iv * 2u;
        else if (fmt < 0xE || fmt > 0xF) pitch = (iv * (attr2 & 0x3Cu)) >> 3;
        else                             pitch = iv * 4u;
    }

    if (pRect) {
        uint32_t left = MEM32(pRect + 0x00), top = MEM32(pRect + 0x04);
        bpp = (MEM8(D3D8_FORMAT_ATTR_TABLE + fmt) & 0x3Cu) >> 3;
        bits += top * pitch + left * bpp;
        fprintf(stderr, "[D3D8] LockRect on surface 0x%08X with a sub-rect "
                        "(%u,%u) - offset is the standard formula, NOT "
                        "transcribed from Lock2DSurface\n", pThis, left, top);
        fflush(stderr);
    }

    MEM32(pLock + 0) = pitch;
    MEM32(pLock + 4) = bits;
    (void)flags;
    D3D8_SHIM_RET(0x00365990u, 16, D3D8_OK);
}

/* 0x003679B0  [D3D]  1 call site(s) */
void sub_003679B0(void)
{
    D3D8_SHIM_RET(0x003679B0u, 20, D3D8_OK);
}

/* 0x00367B90  [D3D]  2 call site(s) */
void sub_00367B90(void)
{
    D3D8_SHIM_RET(0x00367B90u, 12, D3D8_OK);
}

/* 0x00367ED0  [D3D]  1 call site(s) */
void sub_00367ED0(void)
{
    D3D8_SHIM_RET(0x00367ED0u, 12, D3D8_OK);
}

/* 0x00367F30  [D3D]  1 call site(s) */
void sub_00367F30(void)
{
    D3D8_SHIM_RET(0x00367F30u, 8, D3D8_OK);
}

/* 0x00367FF0  [D3D]  1 call site(s) */
void sub_00367FF0(void)
{
    D3D8_SHIM_RET(0x00367FF0u, 20, D3D8_OK);
}

/* 0x00368050  [D3D]  2 call site(s) */
void sub_00368050(void)
{
    D3D8_SHIM_RET(0x00368050u, 24, D3D8_OK);
}

/*
 * 0x003680D0  Direct3D_CreateDevice(Adapter, DeviceType, hFocusWindow,
 *                                   BehaviorFlags, pPresentParams, ppDevice)
 *
 * NOT in the generated shim scaffold, because the recompiler never reached it -
 * see the note below. Added by hand.
 *
 * The original does not allocate a device: it initialises the GLOBAL object at
 * 0x0036CB00 (about 9.4 KB in the D3D data block), sets D3D_g_pDevice to point
 * at it, and hands that same address back through ppDevice. So the guest's
 * "device pointer" is a fixed address, which is what makes this shimmable at
 * all - there is no object identity to forge.
 *
 * What this does:
 *   1. creates the REAL native device (src/d3d), which the gfx harness in
 *      tools/gfx_harness already proved works end to end - window, swap chain,
 *      clear, draw and present
 *   2. gives the guest device block a usable push buffer, because
 *      SetRenderState_Simple at 0x0035D900 reads [0x0036CB00] as the write
 *      pointer and [0x0036CB04] as the limit. Left at zero it takes its
 *      overflow path into the unlifted D3D block and dies. That entry has 36
 *      call sites, so this is not a corner case.
 *   3. returns 0x0036CB00 through ppDevice and S_OK, as the original does
 *
 * The push buffer is a real guest allocation that nothing drains. Commands
 * accumulate until it fills and then wrap - see the note where it is armed.
 */
void sub_003680D0(void)
{
    uint32_t pPresent = MEM32(g_esp + 20);
    uint32_t ppDevice = MEM32(g_esp + 24);
    uint32_t width  = pPresent ? MEM32(pPresent + 0x00) : 640u;
    uint32_t height = pPresent ? MEM32(pPresent + 0x04) : 480u;
    uint32_t pb;

    if (!d3d8_CreateNativeDevice(width, height)) {
        fprintf(stderr, "[D3D8] CreateDevice: native device creation FAILED "
                        "(%ux%u)\n", width, height);
        fflush(stderr);
        if (ppDevice) MEM32(ppDevice) = 0;
        D3D8_SHIM_RET(0x003680D0u, 24, 0x8876086Cu /* D3DERR_INVALIDCALL */);
        return;
    }

    /*
     * Arm the guest push buffer. SetRenderState_Simple appends two dwords per
     * call and checks against the limit; nothing consumes them, so this only
     * has to be large enough that the wrap is rare and the pointer stays
     * inside guest memory. The buffer is deliberately NOT freed - it lives for
     * the process, like the device block it belongs to.
     */
    pb = d3d8_XMemAlloc(0x20000u, 0x64800000u);
    if (pb) {
        MEM32(0x0036CB00u) = pb;
        MEM32(0x0036CB04u) = pb + 0x20000u;
    } else {
        fprintf(stderr, "[D3D8] CreateDevice: could not allocate a guest push "
                        "buffer; SetRenderState_Simple will take its overflow "
                        "path into unlifted code\n");
        fflush(stderr);
    }

    if (ppDevice) MEM32(ppDevice) = 0x0036CB00u;

    fprintf(stderr, "[D3D8] CreateDevice %ux%u -> guest device 0x0036CB00, "
                    "native device live, push buffer 0x%08X\n",
            width, height, pb);
    fflush(stderr);

    D3D8_SHIM_RET(0x003680D0u, 24, D3D8_OK);
}

/*
 * 0x00368BE0  [D3D]  1 call site(s)
 *
 * D3DDevice_Swap(DWORD Flags) - the real frame boundary.
 *
 * The Xbox presents through Swap, not Present, so this is where the frame
 * pump belongs. It used to sit on SetRenderState_Simple (0x0035D900) by
 * mistake; see the note there.
 *
 * One call site, which is what a present should have.
 *
 * d3d8_PresentFrame() is safe to call before any device exists: it always
 * pumps the Windows message queue and only presents when a swap chain has
 * been created. Pumping here keeps the window responsive as soon as one is
 * made, and is the reason this is wired up now rather than after the device
 * creation entries are implemented.
 */
void sub_00368BE0(void)
{
    static unsigned frames;
    frames++;

    d3d8_PresentFrame();

    /*
     * Pace only when nothing else is pacing us. With a device, PresentFrame
     * presents with VSync = 1 and that sets the rate; sleeping on top of it
     * would halve the frame rate rather than cap it. Without a device it does
     * nothing but pump messages, so an unpaced caller would spin a core.
     */
    if (!d3d8_GetHWND()) {
        static DWORD last;
        DWORD now = GetTickCount();
        DWORD elapsed = now - last;
        if (elapsed < 16) Sleep(16 - elapsed);
        last = GetTickCount();
    }

    if (frames <= 4 || (frames % 60) == 0) {
        fprintf(stderr, "[D3D8] Swap (present) frame %u%s\n", frames,
                d3d8_GetHWND() ? "" : " - no device yet, message pump only");
        fflush(stderr);
    }

    D3D8_SHIM_RET(0x00368BE0u, 4, D3D8_OK);
}

/* 0x00368E50  [D3D]  3 call site(s) */
void sub_00368E50(void)
{
    D3D8_SHIM_RET(0x00368E50u, 8, D3D8_OK);
}

/* 0x00368EB0  [D3D]  1 call site(s) */
void sub_00368EB0(void)
{
    D3D8_SHIM_RET(0x00368EB0u, 12, D3D8_OK);
}

/* 0x00368F00  [D3D]  1 call site(s) */
void sub_00368F00(void)
{
    D3D8_SHIM_RET(0x00368F00u, 8, D3D8_OK);
}

/* 0x00368F50  [D3D]  2 call site(s) */
void sub_00368F50(void)
{
    D3D8_SHIM_RET(0x00368F50u, 4, D3D8_OK);
}

/* 0x003692E0  [D3D]  3 call site(s) */
void sub_003692E0(void)
{
    D3D8_SHIM_RET(0x003692E0u, 4, D3D8_OK);
}

/* 0x003694E0  [D3D]  1 call site(s) */
void sub_003694E0(void)
{
    D3D8_SHIM_RET(0x003694E0u, 12, D3D8_OK);
}

/* 0x00396FBD  [D3DX]  1 call site(s) */
void sub_00396FBD(void)
{
    D3D8_SHIM_RET(0x00396FBDu, 40, D3D8_OK);
}

/* 0x00397737  [D3DX]  1 call site(s) */
void sub_00397737(void)
{
    D3D8_SHIM_RET(0x00397737u, 16, D3D8_OK);
}

/* 0x0039788A  [D3DX]  1 call site(s) */
void sub_0039788A(void)
{
    D3D8_SHIM_RET(0x0039788Au, 16, D3D8_OK);
}
