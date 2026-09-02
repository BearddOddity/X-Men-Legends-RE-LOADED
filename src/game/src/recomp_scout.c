/*
 * recomp_scout.c - THROWAWAY reconnaissance build. Not part of the port.
 *
 * ============================================================================
 *  THIS FILE EXISTS TO ANSWER ONE QUESTION AND THEN BE DELETED.
 *
 *  "Do the 29,000 lines of D3D/NV2A/APU host code produce anything at all?"
 *
 *  They have never faced a workload, because the boot dies in static
 *  initialiser 3 of 10 and the graphics runtime is downstream of all ten.
 *  Every wall broken on the faithful path is progress toward finding out.
 *  This build finds out now instead.
 *
 *  NOTHING HERE IS A FIX. Nothing here may be measured, recorded in
 *  progress.json, or cited in the ledger. It deliberately continues past
 *  faults the real port must not continue past. A run of this build proves
 *  nothing about correctness - only about reach.
 * ============================================================================
 *
 * How it works
 * ------------
 * sub_00011E40 is the static initialiser driver: a fixed sequence of ten
 * calls, not a table walk. Initialiser 3 (sub_00239E50, the registry
 * singleton) never returns, so initialisers 4-10 have never executed and
 * neither has anything they set up.
 *
 * scout_static_init() is the lifted body of sub_00011E40 copied verbatim -
 * same guest stack arithmetic, same globals, same order - with each call
 * wrapped in SEH. On a fault it logs which initialiser died and where, then
 * continues to the next one.
 *
 * The body is copied rather than re-derived on purpose. Hand-rolling the
 * cdecl pushes risks a stack imbalance that would fault on its own and be
 * indistinguishable from a real finding. The precedent is sub_001A1C23 in
 * recomp_manual.c, which replays a call sequence by hand for the same reason.
 *
 * This mirrors RECOMP_TRAP_PAGE_ZERO: do not die at the first hit, census the
 * whole set in one boot. One run yields every failing initialiser rather than
 * the first, and then reports how far execution actually got.
 *
 * Build:  cmake -S . -B build_scout -G Ninja -DRECOMP_SCOUT=ON
 * Run:    run_scout.bat        (writes scout_stderr.txt, never stderr.txt)
 */

#ifdef RECOMP_SCOUT

/*
 * windows.h first: the generated header defines lowercase register macros
 * (eax, esp, ...) and pulling it in ahead of the platform headers invites a
 * collision. The generated files get away with it because they include
 * nothing else.
 */
#include <stdio.h>
#include <windows.h>

#define RECOMP_GENERATED_CODE
#include "gen/recomp_funcs.h"

/* Filled in by the SEH filter so the handler can report the fault. */
static unsigned long scout_last_code;
static void        *scout_last_addr;

static int scout_filter(EXCEPTION_POINTERS *ep)
{
    scout_last_code = ep->ExceptionRecord->ExceptionCode;
    scout_last_addr = ep->ExceptionRecord->ExceptionAddress;
    return EXCEPTION_EXECUTE_HANDLER;
}

int  g_scout_init_ok;
int  g_scout_init_failed;

/*
 * Run one initialiser, survive a fault.
 *
 * The guest esp is restored to its pre-call value on fault. The guest's own
 * registers are left as the fault found them: an initialiser that died
 * half-way has already made whatever mess it made, and pretending otherwise
 * would be the sort of invention this project's guard rule exists to prevent.
 */
#define SCOUT_STEP(n, fn)                                                     \
    do {                                                                      \
        uint32_t _saved_esp = g_esp;                                          \
        __try {                                                               \
            RECOMP_ABI_CALL(fn);   /* marks the site, same as the lifted body */ \
            g_scout_init_ok++;                                                \
            fprintf(stderr, "[SCOUT] init %-2d %-14s ok\n", (n), #fn);        \
        } __except (scout_filter(GetExceptionInformation())) {                \
            g_scout_init_failed++;                                            \
            g_esp = _saved_esp;                                               \
            fprintf(stderr,                                                   \
                "[SCOUT] init %-2d %-14s FAULTED code=%08lX at host %p "      \
                "- skipping, guest state left as the fault found it\n",       \
                (n), #fn, scout_last_code, scout_last_addr);                  \
        }                                                                     \
        fflush(stderr);                                                       \
    } while (0)

extern void sub_0011DD40(void);
extern void sub_001E8DE0(void);
extern void sub_00239E50(void);
extern void sub_0011EBB0(void);
extern void sub_00290500(void);
extern void sub_00303FD0(void);
extern void sub_00304AD0(void);
extern void sub_003042B0(void);
extern void sub_00145340(void);
extern void sub_00011320(void);

void scout_static_init(void)
{
    fprintf(stderr,
        "[SCOUT] ==================================================\n"
        "[SCOUT] RECONNAISSANCE BUILD - continues past faults.\n"
        "[SCOUT] Nothing measured here is valid progress.\n"
        "[SCOUT] ==================================================\n");
    fflush(stderr);

    g_scout_init_ok = 0;
    g_scout_init_failed = 0;

    /* --- body of sub_00011E40, copied verbatim, calls wrapped ----------- */

    PUSH32(esp, 0xFFFFFFFFu);
    PUSH32(esp, 0x34E068);
    eax = MEM32(g_fs_base);
    PUSH32(esp, eax);
    MEM32(g_fs_base) = esp;
    PUSH32(esp, ecx);
    SET_LO8(eax, MEM8(0x5024C8));
    PUSH32(esp, ebx);
    ebx = 0; /* xor self */
    MEM8(0x5BC548) = 1;

    if (!CMP_NE(LO8(eax), LO8(ebx))) {
        PUSH32(esp, 0); SCOUT_STEP(1, sub_0011DD40);
    } else {
        fprintf(stderr, "[SCOUT] init 1  sub_0011DD40   skipped by the "
                        "original's own test on MEM8(0x5024C8)\n");
    }

    PUSH32(esp, 0x11A60);
    PUSH32(esp, 0); SCOUT_STEP(2, sub_001E8DE0);

    PUSH32(esp, 0xC80);
    MEM8(0x46A3A5) = LO8(ebx);
    MEM8(0x46A3A6) = LO8(ebx);
    MEM8(0x46A3A8) = LO8(ebx);
    MEM8(0x46A3A9) = LO8(ebx);
    PUSH32(esp, 0); SCOUT_STEP(3, sub_00239E50);   /* the wall */

    esp = esp + 8;
    MEM32(esp + 0x10) = ebx;
    PUSH32(esp, 0); SCOUT_STEP(4, sub_0011EBB0);
    PUSH32(esp, 0); SCOUT_STEP(5, sub_00290500);
    PUSH32(esp, 0); SCOUT_STEP(6, sub_00303FD0);
    PUSH32(esp, 0); SCOUT_STEP(7, sub_00304AD0);
    PUSH32(esp, 0); SCOUT_STEP(8, sub_003042B0);
    PUSH32(esp, 0); SCOUT_STEP(9, sub_00145340);
    PUSH32(esp, 0); SCOUT_STEP(10, sub_00011320);

    /*
     * The tail indirect call: edx = MEM32(eax); ecx = eax; call MEM32(edx).
     * eax comes from initialiser 10, so if that one faulted this reads
     * whatever the fault left behind. Guarded rather than skipped, because
     * whether it is reachable at all is part of what this build measures.
     */
    {
        uint32_t _saved_esp = g_esp;
        __try {
            edx = MEM32(eax);
            ecx = eax;
            {
                uint32_t _icall_esp = g_esp;
                uint32_t _icall_target = MEM32(edx);
                PUSH32(esp, 0);
                RECOMP_ICALL_SAFE(_icall_target, _icall_esp);
            }
            fprintf(stderr, "[SCOUT] tail icall ok\n");
        } __except (scout_filter(GetExceptionInformation())) {
            g_esp = _saved_esp;
            fprintf(stderr, "[SCOUT] tail icall FAULTED code=%08lX at host %p"
                            " (eax=%08X)\n",
                    scout_last_code, scout_last_addr, eax);
        }
        fflush(stderr);
    }

    fprintf(stderr,
        "[SCOUT] static init census: %d ran, %d faulted and were skipped\n",
        g_scout_init_ok, g_scout_init_failed);
    fflush(stderr);
}

#endif /* RECOMP_SCOUT */
