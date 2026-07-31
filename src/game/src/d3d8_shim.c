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
 * D3D8_SHIM_ENTER / D3D8_SHIM_RET
 *
 * ret_imm is the callee's __stdcall argument cleanup, read from the real
 * `ret N` in the XBE. Total esp adjustment is 4 (dummy return address the
 * caller pushed) + ret_imm (the arguments).
 */
#define D3D8_SHIM_RET(ret_imm, value)  \
    do { g_d3d8_shim_calls++;          \
         g_eax = (value);              \
         g_esp += 4 + (ret_imm); } while (0)

/* S_OK - most D3D8 calls that report status succeed trivially in Phase 1. */
#define D3D8_OK 0u

/* 0x0035ADD0  [D3D]  2 call site(s) */
void sub_0035ADD0(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x0035AE60  [D3D]  1 call site(s) */
void sub_0035AE60(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035AE90  [D3D]  3 call site(s) */
void sub_0035AE90(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x0035AFB0  [D3D]  1 call site(s) */
void sub_0035AFB0(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035B030  [D3D]  1 call site(s) */
void sub_0035B030(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035B040  [D3D]  1 call site(s) */
void sub_0035B040(void)
{
    D3D8_SHIM_RET(0, D3D8_OK);
}

/* 0x0035B120  [D3D]  2 call site(s) */
void sub_0035B120(void)
{
    D3D8_SHIM_RET(0, D3D8_OK);
}

/* 0x0035B340  [D3D]  2 call site(s) */
void sub_0035B340(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035B3F0  [D3D]  2 call site(s) */
void sub_0035B3F0(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x0035B670  [D3D]  2 call site(s) */
void sub_0035B670(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035B6C0  [D3D]  1 call site(s) */
void sub_0035B6C0(void)
{
    D3D8_SHIM_RET(20, D3D8_OK);
}

/* 0x0035B9F0  [D3D]  1 call site(s) */
void sub_0035B9F0(void)
{
    D3D8_SHIM_RET(0, D3D8_OK);
}

/* 0x0035BA10  [D3D]  1 call site(s) */
void sub_0035BA10(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035BC40  [D3D]  9 call site(s) */
void sub_0035BC40(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x0035BF00  [D3D]  3 call site(s) */
void sub_0035BF00(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x0035C060  [D3D]  7 call site(s) */
void sub_0035C060(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x0035C210  [D3D]  2 call site(s) */
void sub_0035C210(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x0035C2A0  [D3D]  1 call site(s) */
void sub_0035C2A0(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x0035CFC0  [D3D]  1 call site(s) */
void sub_0035CFC0(void)
{
    D3D8_SHIM_RET(16, D3D8_OK);
}

/* 0x0035D100  [D3D]  1 call site(s) */
void sub_0035D100(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035D160  [D3D]  1 call site(s) */
void sub_0035D160(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035D330  [D3D]  1 call site(s) */
void sub_0035D330(void)
{
    D3D8_SHIM_RET(12, D3D8_OK);
}

/* 0x0035D360  [D3D]  4 call site(s) */
void sub_0035D360(void)
{
    D3D8_SHIM_RET(12, D3D8_OK);
}

/* 0x0035D650  [D3D]  2 call site(s) */
void sub_0035D650(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035D760  [D3D]  3 call site(s) */
void sub_0035D760(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035D900  [D3D]  36 call site(s) */
void sub_0035D900(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x0035DC40  [D3D]  1 call site(s) */
void sub_0035DC40(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035DC80  [D3D]  1 call site(s) */
void sub_0035DC80(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035DCD0  [D3D]  3 call site(s) */
void sub_0035DCD0(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035DD80  [D3D]  2 call site(s) */
void sub_0035DD80(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035DDC0  [D3D]  1 call site(s) */
void sub_0035DDC0(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035DE20  [D3D]  3 call site(s) */
void sub_0035DE20(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035DF10  [D3D]  1 call site(s) */
void sub_0035DF10(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035DFF0  [D3D]  2 call site(s) */
void sub_0035DFF0(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035E170  [D3D]  3 call site(s) */
void sub_0035E170(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x0035E280  [D3D]  10 call site(s) */
void sub_0035E280(void)
{
    D3D8_SHIM_RET(12, D3D8_OK);
}

/* 0x0035E2F0  [D3D]  1 call site(s) */
void sub_0035E2F0(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x0035E330  [D3D]  1 call site(s) */
void sub_0035E330(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x0035EB50  [D3D]  2 call site(s) */
void sub_0035EB50(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035EBE0  [D3D]  2 call site(s) */
void sub_0035EBE0(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x0035EC80  [D3D]  2 call site(s) */
void sub_0035EC80(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x00361360  [D3D]  3 call site(s) */
void sub_00361360(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x003613A0  [D3D]  35 call site(s) */
void sub_003613A0(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x00361560  [D3D]  2 call site(s) */
void sub_00361560(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x003615B0  [D3D]  2 call site(s) */
void sub_003615B0(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x00361600  [D3D]  1 call site(s) */
void sub_00361600(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x00361660  [D3D]  1 call site(s) */
void sub_00361660(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x00362430  [D3D]  1 call site(s) */
void sub_00362430(void)
{
    D3D8_SHIM_RET(24, D3D8_OK);
}

/* 0x003658E0  [D3D]  2 call site(s) */
void sub_003658E0(void)
{
    D3D8_SHIM_RET(16, D3D8_OK);
}

/* 0x00365970  [D3D]  5 call site(s) */
void sub_00365970(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x00365990  [D3D]  1 call site(s) */
void sub_00365990(void)
{
    D3D8_SHIM_RET(16, D3D8_OK);
}

/* 0x003679B0  [D3D]  1 call site(s) */
void sub_003679B0(void)
{
    D3D8_SHIM_RET(20, D3D8_OK);
}

/* 0x00367B90  [D3D]  2 call site(s) */
void sub_00367B90(void)
{
    D3D8_SHIM_RET(12, D3D8_OK);
}

/* 0x00367ED0  [D3D]  1 call site(s) */
void sub_00367ED0(void)
{
    D3D8_SHIM_RET(12, D3D8_OK);
}

/* 0x00367F30  [D3D]  1 call site(s) */
void sub_00367F30(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x00367FF0  [D3D]  1 call site(s) */
void sub_00367FF0(void)
{
    D3D8_SHIM_RET(20, D3D8_OK);
}

/* 0x00368050  [D3D]  2 call site(s) */
void sub_00368050(void)
{
    D3D8_SHIM_RET(24, D3D8_OK);
}

/* 0x00368BE0  [D3D]  1 call site(s) */
void sub_00368BE0(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x00368E50  [D3D]  3 call site(s) */
void sub_00368E50(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x00368EB0  [D3D]  1 call site(s) */
void sub_00368EB0(void)
{
    D3D8_SHIM_RET(12, D3D8_OK);
}

/* 0x00368F00  [D3D]  1 call site(s) */
void sub_00368F00(void)
{
    D3D8_SHIM_RET(8, D3D8_OK);
}

/* 0x00368F50  [D3D]  2 call site(s) */
void sub_00368F50(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x003692E0  [D3D]  3 call site(s) */
void sub_003692E0(void)
{
    D3D8_SHIM_RET(4, D3D8_OK);
}

/* 0x003694E0  [D3D]  1 call site(s) */
void sub_003694E0(void)
{
    D3D8_SHIM_RET(12, D3D8_OK);
}

/* 0x00396FBD  [D3DX]  1 call site(s) */
void sub_00396FBD(void)
{
    D3D8_SHIM_RET(40, D3D8_OK);
}

/* 0x00397737  [D3DX]  1 call site(s) */
void sub_00397737(void)
{
    D3D8_SHIM_RET(16, D3D8_OK);
}

/* 0x0039788A  [D3DX]  1 call site(s) */
void sub_0039788A(void)
{
    D3D8_SHIM_RET(16, D3D8_OK);
}
