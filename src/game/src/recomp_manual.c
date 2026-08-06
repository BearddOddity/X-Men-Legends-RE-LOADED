/**
 * Manual function overrides and ICALL diagnostics
 *
 * This file provides:
 *   - recomp_lookup_manual()  : intercept specific Xbox VAs with hand-written code
 *   - recomp_icall_fail_log() : log when an indirect call target can't be resolved
 *   - ICALL trace ring buffer  : globals used by the RECOMP_ICALL macro
 *
 * The recomp pipeline generates an auto-dispatch table (recomp_lookup) that
 * resolves most function addresses. recomp_lookup_manual() is called FIRST,
 * giving you a chance to override any function with a custom implementation.
 *
 * Common reasons to add manual overrides:
 *   - Trace a function to understand call flow (wrap the generated version)
 *   - Fix a function the lifter translated incorrectly
 *   - Stub out a function that crashes (return early, set eax to a safe value)
 *   - Redirect a function to a native implementation (e.g., skip CRT init)
 *   - Intercept D3D/audio calls for custom rendering or sound
 */

#include <stdio.h>
#include <stdint.h>
#include <windows.h>
#include "xbox_memory_layout.h"

/* Register model, MEM macros, recomp_lookup/recomp_lookup_manual decls.
 * Not compiled as RECOMP_GENERATED_CODE, so eax/esp etc. stay unaliased -
 * this file uses the g_* names directly. */
#include "recomp_types.h"

/* ── ICALL trace ring buffer ───────────────────────────────── */

/*
 * These globals are written by the RECOMP_ICALL macro (defined in
 * recomp_types.h) every time an indirect call is dispatched. When a
 * crash occurs, the VEH handler or recomp_icall_fail_log() can dump
 * the last 16 call targets to help you trace what happened.
 *
 * Already defined in xbox_kernel (src/kernel/xbox_memory_layout.c) -
 * declare extern here instead of redefining.
 */
extern volatile uint32_t g_icall_trace[16];
extern volatile uint32_t g_icall_trace_idx;
extern volatile uint64_t g_icall_count;

/* ── CPUID ─────────────────────────────────────────────────── */

/*
 * The lifter used to emit cpuid as a bare comment. Nothing was written, so
 * eax/ebx/ecx/edx kept whatever the preceding instructions left there, and
 * sub_001F0F70 (the feature detector, 9 cpuid sites) stored that garbage as
 * the CPU feature word. FUN_001ff070 caches it in DAT_005bc644 and dispatches
 * SIMD paths off it - so which code path ran was undefined.
 *
 * We model the Xbox CPU: a Coppermine Pentium III. MMX and SSE, no SSE2,
 * and - importantly - no extended CPUID, which is how 3DNow! is advertised.
 * Returning 0 for leaf 0x80000000 makes the 3DNow! probe fail deterministically,
 * so the game can never enter the pfmul/pfadd path the lifter doesn't implement.
 *
 * ponytail: RECOMP_CPUID_NO_SIMD is the calibration knob. The lifter currently
 * drops ~550 MMX instructions, so if a library takes an MMX path and produces
 * garbage, build with -DRECOMP_CPUID_NO_SIMD=1 to force scalar fallbacks and
 * A/B it. Default is off: report the CPU the game was actually built against,
 * because inventing a CPU that never shipped exercises paths nobody tested.
 */

/* EDX feature bits for leaf 1 on a Coppermine P3:
 *   0..9  FPU VME DE PSE TSC MSR PAE MCE CX8 APIC
 *   11..17 SEP MTRR PGE MCA CMOV PAT PSE36
 *   23    MMX      24 FXSR      25 SSE
 * PSN (18) is left clear so the CPU-serial path is never taken.
 * SSE2 (26) is clear - the P3 does not have it. */
#define CPUID_P3_FEATURES_EDX  0x0383FBFFu
#define CPUID_SIMD_BITS        ((1u << 23) | (1u << 24) | (1u << 25))

void recomp_cpuid(void)
{
    uint32_t leaf = g_eax;
    uint32_t edx  = CPUID_P3_FEATURES_EDX;

#if defined(RECOMP_CPUID_NO_SIMD) && RECOMP_CPUID_NO_SIMD
    edx &= ~CPUID_SIMD_BITS;    /* force scalar fallbacks */
#endif

    switch (leaf) {
    case 0:                     /* max basic leaf + "GenuineIntel" */
        g_eax = 2;
        g_ebx = 0x756E6547u;    /* "Genu" */
        g_edx = 0x49656E69u;    /* "ineI" */
        g_ecx = 0x6C65746Eu;    /* "ntel" */
        break;

    case 1:                     /* signature + feature flags */
        g_eax = 0x0000068Au;    /* family 6, model 8, stepping 10 */
        g_ebx = 0;
        g_ecx = 0;              /* no SSE3 or later */
        g_edx = edx;
        break;

    case 2:                     /* cache descriptors - benign, 1 iteration */
        g_eax = 0x00000001u;
        g_ebx = 0;
        g_ecx = 0;
        g_edx = 0;
        break;

    default:
        /* Includes every 0x8000xxxx leaf. A real P3 has no extended CPUID,
         * so reporting 0 is both accurate and what blocks the 3DNow! path. */
        g_eax = 0;
        g_ebx = 0;
        g_ecx = 0;
        g_edx = 0;
        break;
    }

#if defined(RECOMP_CPUID_PROBE) && RECOMP_CPUID_PROBE
    fprintf(stderr, "[CPUID] leaf=0x%08X -> eax=0x%08X ebx=0x%08X "
                    "ecx=0x%08X edx=0x%08X\n",
            leaf, g_eax, g_ebx, g_ecx, g_edx);
#endif
}

/* ── Manual function overrides ─────────────────────────────── */

/*
 * Return a function pointer to override the given Xbox VA, or NULL
 * to fall through to the auto-generated dispatch table.
 *
 * This is called on every indirect call (RECOMP_ICALL) and every
 * direct call through the dispatch table, so keep it fast. A chain
 * of if-statements on uint32_t compiles to a simple comparison
 * sequence; for large override tables, consider a sorted array
 * with binary search.
 *
 * Examples of common override patterns:
 *
 *   // Trace wrapper: log entry/exit around the generated function
 *   extern void sub_00012345(void);
 *   static void traced_sub_00012345(void) {
 *       fprintf(stderr, "[TRACE] sub_00012345 entered, eax=0x%08X\n", g_eax);
 *       sub_00012345();
 *       fprintf(stderr, "[TRACE] sub_00012345 returned, eax=0x%08X\n", g_eax);
 *   }
 *
 *   // Stub: skip a function entirely (return 0 in eax)
 *   static void stub_00067890(void) {
 *       g_eax = 0;
 *   }
 *
 *   // Fix: replace a broken lifted function with correct C
 *   static void fixed_sub_000ABCDE(void) {
 *       // Read arguments from stack/registers per calling convention
 *       uint32_t arg1 = g_ecx;
 *       uint32_t arg2 = MEM32(g_esp + 4);
 *       // ... correct implementation ...
 *       g_eax = result;
 *   }
 */

/*
 * sub_0019F196: CRT thread-start trampoline (_threadstartex equivalent).
 *
 * Never detected by the disassembler because it's only reachable as a data
 * value (the StartRoutine field passed to PsCreateSystemThreadEx), never
 * as a direct CALL/JMP target - so it has no call_target xref to find it by.
 *
 * Original x86 sets up CRT per-thread TLS/locale state, then:
 *   eax = StartContext1(StartContext2);
 *   PsTerminateSystemThread(eax);   // never returns
 * The TLS setup is CRT bookkeeping our runtime doesn't need. PsTerminateSystemThread
 * is already a no-op return in kernel_bridge.c's synchronous thread model, so this
 * skips straight to calling StartContext1(StartContext2) and returns normally.
 */
static void sub_0019F196(void)
{
    uint32_t ctx1 = MEM32(g_esp + 4);
    uint32_t ctx2 = MEM32(g_esp + 8);

    recomp_func_t fn = recomp_lookup_manual(ctx1);
    if (!fn) fn = recomp_lookup(ctx1);
    if (fn) {
        g_esp -= 4; MEM32(g_esp) = ctx2;
        g_esp -= 4; MEM32(g_esp) = 0;  /* dummy return address */
        fn();
        g_esp += 8;
    } else {
        fprintf(stderr, "[TRAMPOLINE] thread entry 0x%08X not found in dispatch!\n", ctx1);
        fflush(stderr);
    }
}

/*
 * sub_001A1C23: XAPI process startup dispatcher, passed as StartContext1 to
 * the very first PsCreateSystemThreadEx call (the "main thread"). Same
 * undetected-gap problem as sub_0019F196 above - only reachable as a data
 * value, never a direct CALL/JMP target, so the disassembler never found it.
 * This was why the game appeared to do nothing after the thread trampoline
 * fix: recomp_lookup(0x001A1C23) returned NULL and the trampoline silently
 * skipped calling it.
 *
 * Everything THIS function calls (0x1A3639, 0x1A23F3, 0x1A35AC, 0x1A3554,
 * 0x11E40, 0x1A237D) IS a properly detected function, so rather than wait
 * on a disassembler fix, this override just replays the call sequence by
 * hand from the original disassembly:
 *
 *   call 0x1A3639
 *   call 0x1A23F3
 *   [inline TLS-fixup block, gated on a per-thread debug/notify pointer
 *    that's null in our environment - skipped, see below]
 *   call 0x1A35AC
 *   call 0x1A3554
 *   push 0,0,0; call 0x11E40      (cdecl, 3 args)
 *   push 0,1,1; call 0x1A237D     (cdecl, 3 args)
 *   return 0
 *
 * ponytail: the inline TLS-fixup block (fs:[0x20]->[+0x250]->[+0x24], only
 * runs if that chain is non-null) is skipped rather than hand-translated -
 * our fake TIB never populates that field, so the original code's own
 * null-check already takes the skip path on real hardware equivalent state.
 * Revisit if a crash/missing-notification ever traces back here.
 */
extern void sub_001A3639(void);
extern void sub_001A23F3(void);
extern void sub_001A35AC(void);
extern void sub_001A3554(void);
extern void sub_00011E40(void);
extern void sub_001A237D(void);

static void sub_001A1C23(void)
{
    /*
     * NOTE: do NOT add a re-entrancy guard here. This is XAPI process startup
     * and it does re-enter - measured at 71 nested entries, esp falling ~128
     * bytes each time (00F7FF8C, 00F7FF0C, 00F7FE6C, ...), because the port's
     * thread model runs PsCreateSystemThreadEx bodies inline, so a thread
     * created during startup re-enters startup instead of running concurrently.
     *
     * Blocking the re-entry looks obviously right and is badly wrong: those
     * nested passes are how threads actually run here. Tried it - 200 -> 44
     * kernel calls, heap allocs 49 -> 2, failed icalls 6 -> 243.
     *
     * The recursion is not the bug. Re-running _heap_init on every pass is;
     * that is guarded inside sub_001A23F3 instead.
     */
    /*
     * Assign the TLS slot index the Xbox loader would have assigned.
     *
     * The XBE carries a TLS directory, and its tls_index_addr field names the
     * global the loader must fill in. For this title that is 0x005BA794 -
     * exactly the address the lifted code reads and the one Ghidra names
     * XAPILIB___tls_index:
     *
     *     idx   = MEM32(0x5BA794);
     *     array = MEM32(4);                 // fs:[4], see xbox_memory_layout.c
     *     block = MEM32(array + idx * 4);
     *
     * Nothing in the port processes that directory, so the global held garbage
     * - measured -37 - and the lookup indexed 148 bytes *below* the TLS array.
     * We provide a single TLS block, so slot 0 is the right answer.
     *
     * Game-specific by nature (the address comes from this XBE's header), which
     * is why it lives here rather than in the shared runtime. Idempotent, which
     * matters because this startup path re-enters.
     */
    MEM32(0x005BA794) = 0;   /* XBE TLS directory: tls_index_addr */

#define XSTEP(n, f) do { \
        fprintf(stderr, "[XSTEP] -> " #n " " #f "\n"); fflush(stderr); \
        f(); \
        fprintf(stderr, "[XSTEP] <- " #n " " #f " ok\n"); fflush(stderr); \
    } while (0)

    XSTEP(1, sub_001A3639);
    XSTEP(2, sub_001A23F3);
    XSTEP(3, sub_001A35AC);
    XSTEP(4, sub_001A3554);

    g_esp -= 4; MEM32(g_esp) = 0;
    g_esp -= 4; MEM32(g_esp) = 0;
    g_esp -= 4; MEM32(g_esp) = 0;
    g_esp -= 4; MEM32(g_esp) = 0;  /* dummy return address */
    { static unsigned _e; fprintf(stderr,
        "[MAINLOOP] enter #%u\n", ++_e); fflush(stderr); }
    sub_00011E40();
    { static unsigned _x; fprintf(stderr,
        "[MAINLOOP] RETURNED #%u eax=%08X\n", ++_x, g_eax); fflush(stderr); }
    g_esp += 16;

    g_esp -= 4; MEM32(g_esp) = 0;
    g_esp -= 4; MEM32(g_esp) = 1;
    g_esp -= 4; MEM32(g_esp) = 1;
    g_esp -= 4; MEM32(g_esp) = 0;  /* dummy return address */
    sub_001A237D();
    g_esp += 16;

    g_eax = 0;
}

/*
 * sub_001A237D = XAPILIB__XapiBootToDash - quit to the Xbox dashboard.
 *
 * There is no dashboard to quit to here. Left alone it calls
 * XAPILIB__XLaunchNewImageA (sub_0019EB69), which relaunches the title and
 * re-enters process startup - a relaunch loop. Measured: 381 nested startup
 * passes, 49 heaps created before RtlCreateHeap ran the console out of memory,
 * and ~47.6M indirect dispatches before the watchdog fired.
 *
 * Worse, it hides the real fault. The loop is a *consequence* of something
 * during startup deciding to bail; masking it made the failure look like a
 * lifter bug in the allocator for most of a session.
 *
 * Stopping here makes the first bail-out visible and terminal instead of
 * cyclic. Not a fix for whatever bails - a fix for the loop that hid it.
 */
static void sub_001A237D_stub(void)
{
    static unsigned hits;
    fprintf(stderr,
            "[BOOTTODASH] XapiBootToDash called (#%u) - the title is trying to "
            "quit to the dashboard. Stubbed; not relaunching.\n", ++hits);
    fflush(stderr);
    g_eax = 0;
}

/* ── Seeded functions (tools_data/seed_missing_functions.py) ── */
/*
 * Reachable only via data-section pointers, so function discovery
 * missed them and every indirect call here returned NULL. Declared
 * here rather than in the generated recomp_funcs.h so the wiring
 * survives a regeneration.
 */
extern void sub_00011B35(void);
extern void sub_00011EC0(void);
extern void sub_000147ED(void);
extern void sub_0001486F(void);
extern void sub_000149DF(void);
extern void sub_00014C60(void);
extern void sub_00014CC0(void);
extern void sub_00014DAF(void);
extern void sub_00014E9F(void);
extern void sub_0001511F(void);
extern void sub_0001518D(void);
extern void sub_000151EF(void);
extern void sub_000152AF(void);
extern void sub_0001531F(void);
extern void sub_00015868(void);
extern void sub_000158E8(void);
extern void sub_00015BAE(void);
extern void sub_00015C36(void);
extern void sub_000162E0(void);
extern void sub_0001661C(void);
extern void sub_000189B4(void);
extern void sub_000189F8(void);
extern void sub_00018CB5(void);
extern void sub_00019880(void);
extern void sub_0001990A(void);
extern void sub_00019C00(void);
extern void sub_0001C577(void);
extern void sub_0001D872(void);
extern void sub_0001D9E6(void);
extern void sub_0001E0CF(void);
extern void sub_0001E0EB(void);
extern void sub_0001EDCB(void);
extern void sub_0001F315(void);
extern void sub_0001F989(void);
extern void sub_0001FE5E(void);
extern void sub_000203B1(void);
extern void sub_00020856(void);
extern void sub_00021582(void);
extern void sub_00021DD4(void);
extern void sub_0002581F(void);
extern void sub_0002668C(void);
extern void sub_0002719C(void);
extern void sub_00028525(void);
extern void sub_00028814(void);
extern void sub_000298FF(void);
extern void sub_0002B1E0(void);
extern void sub_00030B3B(void);
extern void sub_00034EE9(void);
extern void sub_000362AF(void);
extern void sub_000366F4(void);
extern void sub_00036DCC(void);
extern void sub_00038909(void);
extern void sub_00038A9D(void);
extern void sub_000390E9(void);
extern void sub_000390F5(void);
extern void sub_00039C1F(void);
extern void sub_0003C7A7(void);
extern void sub_0003E103(void);
extern void sub_0003F54C(void);
extern void sub_0003FACE(void);
extern void sub_00040304(void);
extern void sub_0004353D(void);
extern void sub_00047D19(void);
extern void sub_0004834A(void);
extern void sub_000488CD(void);
extern void sub_000491EA(void);
extern void sub_000493D4(void);
extern void sub_000498FD(void);
extern void sub_00049E19(void);
extern void sub_0004DB20(void);
extern void sub_0004E2F6(void);
extern void sub_0004E660(void);
extern void sub_0004ED62(void);
extern void sub_0004EE28(void);
extern void sub_0004FCD7(void);
extern void sub_000501C9(void);
extern void sub_00050762(void);
extern void sub_00050CF2(void);
extern void sub_00051B50(void);
extern void sub_00051FE1(void);
extern void sub_000527FF(void);
extern void sub_0005590D(void);
extern void sub_00055A0F(void);
extern void sub_0005616C(void);
extern void sub_000573CD(void);
extern void sub_000578F0(void);
extern void sub_00058AAC(void);
extern void sub_0005A0C8(void);
extern void sub_0005A0D0(void);
extern void sub_0005B4FE(void);
extern void sub_0005B502(void);
extern void sub_0005C0B5(void);
extern void sub_0005C1DA(void);
extern void sub_0005C61B(void);
extern void sub_0005CE0B(void);
extern void sub_0005F5FD(void);
extern void sub_0005F83D(void);
extern void sub_0005F8DD(void);
extern void sub_00060239(void);
extern void sub_000602EB(void);
extern void sub_000603EE(void);
extern void sub_000643BB(void);
extern void sub_0006702D(void);
extern void sub_0006727E(void);
extern void sub_00067508(void);
extern void sub_00067AA9(void);
extern void sub_00067EA8(void);
extern void sub_000681CD(void);
extern void sub_0006822F(void);
extern void sub_00068325(void);
extern void sub_000684D9(void);
extern void sub_0006882F(void);
extern void sub_0006891C(void);
extern void sub_0006A07B(void);
extern void sub_0006A0BC(void);
extern void sub_0006A2D9(void);
extern void sub_0006AAB0(void);
extern void sub_0006C7BD(void);
extern void sub_0006CE84(void);
extern void sub_0006DC4F(void);
extern void sub_0006F6D1(void);
extern void sub_000707C2(void);
extern void sub_000707CF(void);
extern void sub_00070C20(void);
extern void sub_00070CFB(void);
extern void sub_00071F12(void);
extern void sub_00075F1B(void);
extern void sub_000763FC(void);
extern void sub_00076E07(void);
extern void sub_00077719(void);
extern void sub_00077916(void);
extern void sub_0007816F(void);
extern void sub_0007922D(void);
extern void sub_000795E8(void);
extern void sub_0007B724(void);
extern void sub_0007B774(void);
extern void sub_0007DDF8(void);
extern void sub_0007E1C6(void);
extern void sub_0007FC15(void);
extern void sub_00082854(void);
extern void sub_000828CB(void);
extern void sub_00082BB1(void);
extern void sub_00082BB3(void);
extern void sub_0008324D(void);
extern void sub_0008395D(void);
extern void sub_00083BA9(void);
extern void sub_00083D52(void);
extern void sub_00083F1C(void);
extern void sub_00084488(void);
extern void sub_00086727(void);
extern void sub_00086B27(void);
extern void sub_00086D2E(void);
extern void sub_000882BE(void);
extern void sub_00088942(void);
extern void sub_00088D13(void);
extern void sub_00088E44(void);
extern void sub_00088ED5(void);
extern void sub_00089B5D(void);
extern void sub_00089C8F(void);
extern void sub_00089D55(void);
extern void sub_00089E20(void);
extern void sub_0008CBD8(void);
extern void sub_0008D8D0(void);
extern void sub_0008DA4E(void);
extern void sub_0008DBBD(void);
extern void sub_0008EFAD(void);
extern void sub_0008F2CF(void);
extern void sub_00090BB9(void);
extern void sub_0009175D(void);
extern void sub_00091EAB(void);
extern void sub_0009329D(void);
extern void sub_00093646(void);
extern void sub_00093ED7(void);
extern void sub_00094C26(void);
extern void sub_0009506F(void);
extern void sub_0009515D(void);
extern void sub_00099C70(void);
extern void sub_00099CDE(void);
extern void sub_0009C9E4(void);
extern void sub_0009E60F(void);
extern void sub_0009F092(void);
extern void sub_000A089F(void);
extern void sub_000A108D(void);
extern void sub_000A1A40(void);
extern void sub_000A1C00(void);
extern void sub_000A21C3(void);
extern void sub_000A2A93(void);
extern void sub_000A3D3B(void);
extern void sub_000A3E3F(void);
extern void sub_000A4714(void);
extern void sub_000A589B(void);
extern void sub_000A6672(void);
extern void sub_000A6675(void);
extern void sub_000A9F3F(void);
extern void sub_000AC856(void);
extern void sub_000AD23D(void);
extern void sub_000ADD0B(void);
extern void sub_000B10AF(void);
extern void sub_000B1A9D(void);
extern void sub_000B1AFD(void);
extern void sub_000B1FD0(void);
extern void sub_000B2ECF(void);
extern void sub_000B2F7C(void);
extern void sub_000B3D8F(void);
extern void sub_000B3DFF(void);
extern void sub_000B416D(void);
extern void sub_000B46E2(void);
extern void sub_000B55A5(void);
extern void sub_000B5A85(void);
extern void sub_000B5A90(void);
extern void sub_000B5C9C(void);
extern void sub_000B636E(void);
extern void sub_000B7220(void);
extern void sub_000B757F(void);
extern void sub_000B7851(void);
extern void sub_000B7D4D(void);
extern void sub_000B7E39(void);
extern void sub_000B8C4A(void);
extern void sub_000B9182(void);
extern void sub_000BA4E2(void);
extern void sub_000BA92A(void);
extern void sub_000BB11D(void);
extern void sub_000BB1DD(void);
extern void sub_000BCEA9(void);
extern void sub_000BD66F(void);
extern void sub_000BD73F(void);
extern void sub_000BD7AF(void);
extern void sub_000BD99C(void);
extern void sub_000BED70(void);
extern void sub_000BF2F3(void);
extern void sub_000BF3CF(void);
extern void sub_000BFDFF(void);
extern void sub_000C0801(void);
extern void sub_000C0815(void);
extern void sub_000C08D3(void);
extern void sub_000C08E7(void);
extern void sub_000C0B60(void);
extern void sub_000C1BE3(void);
extern void sub_000C3E85(void);
extern void sub_000C859E(void);
extern void sub_000C9FCD(void);
extern void sub_000CA17D(void);
extern void sub_000CA1DF(void);
extern void sub_000CA4BF(void);
extern void sub_000CA52D(void);
extern void sub_000CA58D(void);
extern void sub_000CA5EF(void);
extern void sub_000CA65F(void);
extern void sub_000CA6CD(void);
extern void sub_000CA75F(void);
extern void sub_000CB39C(void);
extern void sub_000CB439(void);
extern void sub_000CB4FC(void);
extern void sub_000CFBCF(void);
extern void sub_000CFF7C(void);
extern void sub_000D07A0(void);
extern void sub_000D10A1(void);
extern void sub_000D13BA(void);
extern void sub_000D3DCD(void);
extern void sub_000D5642(void);
extern void sub_000D62C1(void);
extern void sub_000D62F1(void);
extern void sub_000D87E2(void);
extern void sub_000DADBD(void);
extern void sub_000DC125(void);
extern void sub_000DC2FE(void);
extern void sub_000DC300(void);
extern void sub_000DCCE6(void);
extern void sub_000E220D(void);
extern void sub_000E306F(void);
extern void sub_000E30DF(void);
extern void sub_000E3549(void);
extern void sub_000E3A10(void);
extern void sub_000E7A2D(void);
extern void sub_000E98FD(void);
extern void sub_000E9C74(void);
extern void sub_000EA51F(void);
extern void sub_000EAA2F(void);
extern void sub_000EB740(void);
extern void sub_000EB756(void);
extern void sub_000EB7B2(void);
extern void sub_000EBA2C(void);
extern void sub_000EBD30(void);
extern void sub_000EBD97(void);
extern void sub_000EC6FE(void);
extern void sub_000ECBFB(void);
extern void sub_000F116B(void);
extern void sub_000F241B(void);
extern void sub_000F68ED(void);
extern void sub_000F694D(void);
extern void sub_000F6B28(void);
extern void sub_000F6BE5(void);
extern void sub_000F7BD6(void);
extern void sub_000F7D4E(void);
extern void sub_000F7E6D(void);
extern void sub_000F7ECD(void);
extern void sub_000FCB1F(void);
extern void sub_000FD42C(void);
extern void sub_000FD731(void);
extern void sub_000FD7BB(void);
extern void sub_000FE7E3(void);
extern void sub_000FEA30(void);
extern void sub_000FF085(void);
extern void sub_000FF087(void);
extern void sub_0010168D(void);
extern void sub_0010262C(void);
extern void sub_0010617D(void);
extern void sub_0010632E(void);
extern void sub_00106DD0(void);
extern void sub_001072FE(void);
extern void sub_00108486(void);
extern void sub_0010901F(void);
extern void sub_0010A30B(void);
extern void sub_0010CF2E(void);
extern void sub_0010DA10(void);
extern void sub_0010E75E(void);
extern void sub_0011316F(void);
extern void sub_0011742D(void);
extern void sub_001183ED(void);
extern void sub_00118839(void);
extern void sub_0011B58F(void);
extern void sub_0011B60F(void);
extern void sub_0011C44B(void);
extern void sub_0011C53D(void);
extern void sub_0011E13D(void);
extern void sub_0011FB69(void);
extern void sub_00120070(void);
extern void sub_0012176D(void);
extern void sub_0012183D(void);
extern void sub_00121E39(void);
extern void sub_00121EC9(void);
extern void sub_001238DA(void);
extern void sub_00123B35(void);
extern void sub_0012410D(void);
extern void sub_00124399(void);
extern void sub_001245B9(void);
extern void sub_001248BF(void);
extern void sub_00125050(void);
extern void sub_00126754(void);
extern void sub_00126834(void);
extern void sub_00126944(void);
extern void sub_00126A34(void);
extern void sub_001271ED(void);
extern void sub_00128204(void);
extern void sub_00128394(void);
extern void sub_001286C8(void);
extern void sub_001286E2(void);
extern void sub_0012A8F3(void);
extern void sub_0012DB5F(void);
extern void sub_0012E44D(void);
extern void sub_0012ECB0(void);
extern void sub_0012ECD5(void);
extern void sub_0012EE50(void);
extern void sub_0012EE5E(void);
extern void sub_0012F563(void);
extern void sub_0012F8F0(void);
extern void sub_001303AE(void);
extern void sub_00130BB7(void);
extern void sub_00130BC0(void);
extern void sub_00130BCE(void);
extern void sub_001315B1(void);
extern void sub_001315BF(void);
extern void sub_00131892(void);
extern void sub_001318A0(void);
extern void sub_00131B3F(void);
extern void sub_00132310(void);
extern void sub_00132335(void);
extern void sub_00132370(void);
extern void sub_00132395(void);
extern void sub_001323D0(void);
extern void sub_001323F5(void);
extern void sub_00134AFD(void);
extern void sub_001352B9(void);
extern void sub_00135E8F(void);
extern void sub_00135F8F(void);
extern void sub_0013608F(void);
extern void sub_0013618F(void);
extern void sub_0013632F(void);
extern void sub_001364EF(void);
extern void sub_00138A5F(void);
extern void sub_001392B1(void);
extern void sub_0013939C(void);
extern void sub_00139D5C(void);
extern void sub_0013A238(void);
extern void sub_0013A23D(void);
extern void sub_0013C94D(void);
extern void sub_0013CC14(void);
extern void sub_0013CCE0(void);
extern void sub_0013E974(void);
extern void sub_0013EECA(void);
extern void sub_0013EFBA(void);
extern void sub_0014388F(void);
extern void sub_001454BD(void);
extern void sub_00146BDF(void);
extern void sub_00146CDF(void);
extern void sub_00148797(void);
extern void sub_001489BD(void);
extern void sub_0014992F(void);
extern void sub_0014BF1D(void);
extern void sub_0014E98F(void);
extern void sub_0014EAFF(void);
extern void sub_0014EB7F(void);
extern void sub_0014EBEF(void);
extern void sub_0014ECBD(void);
extern void sub_0014F05C(void);
extern void sub_0014F0EC(void);
extern void sub_0014F179(void);
extern void sub_0014F26C(void);
extern void sub_0014F31C(void);
extern void sub_00150D9C(void);
extern void sub_00152D60(void);
extern void sub_00155FAB(void);
extern void sub_00156012(void);
extern void sub_00156070(void);
extern void sub_001560DD(void);
extern void sub_001561BD(void);
extern void sub_001562DD(void);
extern void sub_001564C5(void);
extern void sub_0015729D(void);
extern void sub_001572A0(void);
extern void sub_001574BD(void);
extern void sub_00157547(void);
extern void sub_00157550(void);
extern void sub_0015777D(void);
extern void sub_00157930(void);
extern void sub_00158972(void);
extern void sub_00158A72(void);
extern void sub_0015A29D(void);
extern void sub_0015B393(void);
extern void sub_00161D5F(void);
extern void sub_00162BF9(void);
extern void sub_00163D7C(void);
extern void sub_00166330(void);
extern void sub_0016639D(void);
extern void sub_00166EB2(void);
extern void sub_0016817C(void);
extern void sub_00168B18(void);
extern void sub_00169070(void);
extern void sub_0016A405(void);
extern void sub_0016A470(void);
extern void sub_0016B944(void);
extern void sub_0016BF05(void);
extern void sub_0016C019(void);
extern void sub_0016C52F(void);
extern void sub_0016CC46(void);
extern void sub_0016F150(void);
extern void sub_0016FD69(void);
extern void sub_001701F5(void);
extern void sub_00170989(void);
extern void sub_00171CA3(void);
extern void sub_001726DD(void);
extern void sub_00174E00(void);
extern void sub_00174E04(void);
extern void sub_00176BBD(void);
extern void sub_00177A30(void);
extern void sub_00177DA9(void);
extern void sub_00177E09(void);
extern void sub_00177E62(void);
extern void sub_00178082(void);
extern void sub_001780E0(void);
extern void sub_0017817B(void);
extern void sub_00178230(void);
extern void sub_0017859D(void);
extern void sub_001789ED(void);
extern void sub_00178A06(void);
extern void sub_00178CB8(void);
extern void sub_00178F6E(void);
extern void sub_0017A53F(void);
extern void sub_0017A634(void);
extern void sub_0017D72F(void);
extern void sub_0017E5ED(void);
extern void sub_0017EEDB(void);
extern void sub_00182DCD(void);
extern void sub_00182FFF(void);
extern void sub_00183379(void);
extern void sub_001860F6(void);
extern void sub_00186B95(void);
extern void sub_00186F1A(void);
extern void sub_001874C0(void);
extern void sub_00188D8D(void);
extern void sub_0018A18F(void);
extern void sub_0018A550(void);
extern void sub_0018AA5F(void);
extern void sub_0018BA51(void);
extern void sub_0018C82D(void);
extern void sub_0018E9D0(void);
extern void sub_0018F3E3(void);
extern void sub_0018FC24(void);
extern void sub_001900A0(void);
extern void sub_001917FA(void);
extern void sub_00191B19(void);
extern void sub_00191D32(void);
extern void sub_00192952(void);
extern void sub_00192956(void);
extern void sub_00193850(void);
extern void sub_00194783(void);
extern void sub_001967B1(void);
extern void sub_001970DD(void);
extern void sub_00198725(void);
extern void sub_00198978(void);
extern void sub_00198EAD(void);
extern void sub_0019961A(void);
extern void sub_0019AD3C(void);
extern void sub_0019CC8D(void);
extern void sub_0019D07C(void);
extern void sub_0019D5DF(void);
extern void sub_0019E00B(void);
extern void sub_0019E082(void);
extern void sub_0019E096(void);
extern void sub_0019E103(void);
extern void sub_0019E17A(void);
extern void sub_0019E49E(void);
extern void sub_0019E4D0(void);
extern void sub_0019E7AE(void);
extern void sub_0019E99F(void);
extern void sub_0019EB99(void);
extern void sub_0019EE3D(void);
extern void sub_0019EF2D(void);
extern void sub_0019EF88(void);
extern void sub_0019F0E3(void);
extern void sub_0019F193(void);
extern void sub_0019F306(void);
extern void sub_0019F39F(void);
extern void sub_0019F41D(void);
extern void sub_0019F7A2(void);
extern void sub_0019F7A4(void);
extern void sub_0019F7DC(void);
extern void sub_0019FE96(void);
extern void sub_001A0163(void);
extern void sub_001A04C3(void);
extern void sub_001A1451(void);
extern void sub_001A2466(void);
extern void sub_001A2878(void);
extern void sub_001A297B(void);
extern void sub_001A2D64(void);
extern void sub_001A3325(void);
extern void sub_001A3B58(void);
extern void sub_001A3BE7(void);
extern void sub_001A44CA(void);
extern void sub_001A62D5(void);
extern void sub_001A6345(void);
extern void sub_001A7600(void);
extern void sub_001AC5A2(void);
extern void sub_001AC5AC(void);
extern void sub_001AC5B2(void);
extern void sub_001ACCC6(void);
extern void sub_001ACEAC(void);
extern void sub_001AE58C(void);
extern void sub_001AECA8(void);
extern void sub_001B00EC(void);
extern void sub_001B2620(void);
extern void sub_001B5CB9(void);
extern void sub_001B5D55(void);
extern void sub_001B5E59(void);
extern void sub_001B5EF5(void);
extern void sub_001B635F(void);
extern void sub_001B673F(void);
extern void sub_001B8480(void);
extern void sub_001BBF30(void);
extern void sub_001BC091(void);
extern void sub_001BC4CF(void);
extern void sub_001BCB96(void);
extern void sub_001BCD10(void);
extern void sub_001BCD42(void);
extern void sub_001BDDD9(void);
extern void sub_001C09F0(void);
extern void sub_001C0BF6(void);
extern void sub_001C0C36(void);
extern void sub_001C1190(void);
extern void sub_001C1301(void);
extern void sub_001C1EC0(void);
extern void sub_001C246C(void);
extern void sub_001C9637(void);
extern void sub_001CCCFD(void);
extern void sub_001CD301(void);
extern void sub_001CED3D(void);
extern void sub_001CEDB5(void);
extern void sub_001D0650(void);
extern void sub_001D0676(void);
extern void sub_001D2E0E(void);
extern void sub_001D350E(void);
extern void sub_001D3615(void);
extern void sub_001D3798(void);
extern void sub_001D37E6(void);
extern void sub_001D3BA5(void);
extern void sub_001DACDF(void);
extern void sub_001DC79D(void);
extern void sub_001DD74A(void);
extern void sub_001E1F58(void);
extern void sub_001E26F9(void);
extern void sub_001E4F9A(void);
extern void sub_001E55A2(void);
extern void sub_001E5EC0(void);
extern void sub_001E7F50(void);
extern void sub_001E8682(void);
extern void sub_001EC0D9(void);
extern void sub_001EC0E4(void);
extern void sub_001EC5E0(void);
extern void sub_001EC750(void);
extern void sub_001ECA9D(void);
extern void sub_001ED185(void);
extern void sub_001ED2E7(void);
extern void sub_001EF6F8(void);
extern void sub_001F51CC(void);
extern void sub_001F51CF(void);
extern void sub_001F5633(void);
extern void sub_001F56A8(void);
extern void sub_001F5804(void);
extern void sub_001FBD3C(void);
extern void sub_001FC17B(void);
extern void sub_001FC457(void);
extern void sub_001FCE30(void);
extern void sub_001FD034(void);
extern void sub_001FE8A0(void);
extern void sub_001FE8E7(void);
extern void sub_002025B2(void);
extern void sub_002025DA(void);
extern void sub_00202890(void);
extern void sub_002028B8(void);
extern void sub_00202A31(void);
extern void sub_00202A59(void);
extern void sub_00202D89(void);
extern void sub_00204967(void);
extern void sub_00204999(void);
extern void sub_00204DD7(void);
extern void sub_00204DE2(void);
extern void sub_00206E2E(void);
extern void sub_0020E493(void);
extern void sub_002126F6(void);
extern void sub_00215B7D(void);
extern void sub_0021624C(void);
extern void sub_0021644B(void);
extern void sub_0021679F(void);
extern void sub_00218360(void);
extern void sub_0021837A(void);
extern void sub_0021A696(void);
extern void sub_0021A9D4(void);
extern void sub_0021AB18(void);
extern void sub_0021B011(void);
extern void sub_0021B16B(void);
extern void sub_0021D19B(void);
extern void sub_0021D5BF(void);
extern void sub_0021D893(void);
extern void sub_0021E370(void);
extern void sub_0021E3A5(void);
extern void sub_0021E595(void);
extern void sub_0021EA41(void);
extern void sub_0021F41F(void);
extern void sub_002242D1(void);
extern void sub_00225EF1(void);
extern void sub_00227F50(void);
extern void sub_0022CEC5(void);
extern void sub_0022D08F(void);
extern void sub_0022DA30(void);
extern void sub_0022DA9C(void);
extern void sub_0022EBB0(void);
extern void sub_0022F70F(void);
extern void sub_0023037D(void);
extern void sub_002303B0(void);
extern void sub_0023065D(void);
extern void sub_00231C6A(void);
extern void sub_002337E3(void);
extern void sub_00235002(void);
extern void sub_0023575C(void);
extern void sub_00235DD3(void);
extern void sub_002366BC(void);
extern void sub_002370B0(void);
extern void sub_00237F7C(void);
extern void sub_002381AD(void);
extern void sub_00238309(void);
extern void sub_00238887(void);
extern void sub_0023A7B0(void);
extern void sub_0023B9FF(void);
extern void sub_0023BF80(void);
extern void sub_0023C170(void);
extern void sub_0023C65F(void);
extern void sub_0023E000(void);
extern void sub_0023E7E0(void);
extern void sub_0023F8C0(void);
extern void sub_002446E6(void);
extern void sub_00246030(void);
extern void sub_00246090(void);
extern void sub_002460F0(void);
extern void sub_00246150(void);
extern void sub_00246560(void);
extern void sub_00246840(void);
extern void sub_00246E1B(void);
extern void sub_002471B0(void);
extern void sub_0024737A(void);
extern void sub_0024BE00(void);
extern void sub_00257DEA(void);
extern void sub_00257E79(void);
extern void sub_002585AD(void);
extern void sub_00259E00(void);
extern void sub_00259EB0(void);
extern void sub_00259EF8(void);
extern void sub_0025A249(void);
extern void sub_0025A5D4(void);
extern void sub_0025A6E4(void);
extern void sub_0025AB29(void);
extern void sub_0025DBED(void);
extern void sub_00262405(void);
extern void sub_00262410(void);
extern void sub_00262522(void);
extern void sub_0026256A(void);
extern void sub_00266CC8(void);
extern void sub_002681BF(void);
extern void sub_00268F41(void);
extern void sub_00268FEA(void);
extern void sub_002698BD(void);
extern void sub_0026A17E(void);
extern void sub_0026B7D4(void);
extern void sub_0026E425(void);
extern void sub_0026FD01(void);
extern void sub_00271AF6(void);
extern void sub_00272DE4(void);
extern void sub_00273FC6(void);
extern void sub_00278278(void);
extern void sub_00278ECD(void);
extern void sub_002792C0(void);
extern void sub_00279D56(void);
extern void sub_00279E25(void);
extern void sub_00279E58(void);
extern void sub_00279F59(void);
extern void sub_0027A422(void);
extern void sub_0027B1D0(void);
extern void sub_0027B9B0(void);
extern void sub_0027C813(void);
extern void sub_0027CF1C(void);
extern void sub_0027D4BA(void);
extern void sub_0027D724(void);
extern void sub_0027D982(void);
extern void sub_0027DA90(void);
extern void sub_0027DC0A(void);
extern void sub_0027E327(void);
extern void sub_0027F326(void);
extern void sub_0027F3A7(void);
extern void sub_002804F0(void);
extern void sub_00282547(void);
extern void sub_00285DB0(void);
extern void sub_00287FA2(void);
extern void sub_002895A0(void);
extern void sub_0029CE1D(void);
extern void sub_0029D1B4(void);
extern void sub_0029D368(void);
extern void sub_0029D671(void);
extern void sub_002A16BB(void);
extern void sub_002A43A6(void);
extern void sub_002A4FF7(void);
extern void sub_002A53B2(void);
extern void sub_002A548C(void);
extern void sub_002A5E17(void);
extern void sub_002A70CC(void);
extern void sub_002A7D56(void);
extern void sub_002A9528(void);
extern void sub_002AA355(void);
extern void sub_002AC918(void);
extern void sub_002AFED8(void);
extern void sub_002AFEEA(void);
extern void sub_002B094F(void);
extern void sub_002B269A(void);
extern void sub_002B32FE(void);
extern void sub_002B493C(void);
extern void sub_002B51BF(void);
extern void sub_002B52CD(void);
extern void sub_002B8DFC(void);
extern void sub_002B9034(void);
extern void sub_002BB2FE(void);
extern void sub_002BB8DF(void);
extern void sub_002BC591(void);
extern void sub_002BEC39(void);
extern void sub_002C0EB9(void);
extern void sub_002C0F75(void);
extern void sub_002C15CC(void);
extern void sub_002C1E88(void);
extern void sub_002C207E(void);
extern void sub_002C2A5C(void);
extern void sub_002C2B7E(void);
extern void sub_002C2B80(void);
extern void sub_002C2DD6(void);
extern void sub_002C3073(void);
extern void sub_002C307A(void);
extern void sub_002C318C(void);
extern void sub_002C3193(void);
extern void sub_002C3288(void);
extern void sub_002C36AF(void);
extern void sub_002C3881(void);
extern void sub_002C3AB5(void);
extern void sub_002C3C51(void);
extern void sub_002C7867(void);
extern void sub_002C78C5(void);
extern void sub_002C78C8(void);
extern void sub_002C7B40(void);
extern void sub_002C7B85(void);
extern void sub_002C85DA(void);
extern void sub_002C87A3(void);
extern void sub_002C87A8(void);
extern void sub_002C87AF(void);
extern void sub_002C9D75(void);
extern void sub_002C9D94(void);
extern void sub_002CA27E(void);
extern void sub_002CA7C7(void);
extern void sub_002CAB8D(void);
extern void sub_002CB2B6(void);
extern void sub_002CB2B8(void);
extern void sub_002CB305(void);
extern void sub_002CB348(void);
extern void sub_002CB368(void);
extern void sub_002CC832(void);
extern void sub_002CCC27(void);
extern void sub_002CCE2A(void);
extern void sub_002CCE53(void);
extern void sub_002CCEC0(void);
extern void sub_002CD068(void);
extern void sub_002CD091(void);
extern void sub_002CD5FD(void);
extern void sub_002CD6B2(void);
extern void sub_002CDEE8(void);
extern void sub_002CDEF6(void);
extern void sub_002CE193(void);
extern void sub_002CE1A7(void);
extern void sub_002CE7F3(void);
extern void sub_002CEA69(void);
extern void sub_002CEBAC(void);
extern void sub_002CF238(void);
extern void sub_002CF26E(void);
extern void sub_002CF2E4(void);
extern void sub_002CF5F3(void);
extern void sub_002D3140(void);
extern void sub_002D33AC(void);
extern void sub_002D88D2(void);
extern void sub_002DFBA0(void);
extern void sub_002DFC47(void);
extern void sub_002E487C(void);
extern void sub_002E4883(void);
extern void sub_002E495E(void);
extern void sub_002E4F3C(void);
extern void sub_002E4F43(void);
extern void sub_002E501E(void);
extern void sub_002E5DA4(void);
extern void sub_002E5DC5(void);
extern void sub_002E6506(void);
extern void sub_002E7073(void);
extern void sub_002E7200(void);
extern void sub_002E72FE(void);
extern void sub_002E73B0(void);
extern void sub_002E73DC(void);
extern void sub_002E88ED(void);
extern void sub_002EE4B1(void);
extern void sub_002EE6A1(void);
extern void sub_002EE6A2(void);
extern void sub_002EE843(void);
extern void sub_002EE844(void);
extern void sub_002EE892(void);
extern void sub_002EF134(void);
extern void sub_002EF4C3(void);
extern void sub_002EF4F3(void);
extern void sub_002EF938(void);
extern void sub_002EFCFE(void);
extern void sub_002EFFE3(void);
extern void sub_002F032A(void);
extern void sub_002F0334(void);
extern void sub_002F0344(void);
extern void sub_002F04ED(void);
extern void sub_002F1336(void);
extern void sub_002F1350(void);
extern void sub_002F1352(void);
extern void sub_002F13A6(void);
extern void sub_002F14ED(void);
extern void sub_002F1A7B(void);
extern void sub_002F269A(void);
extern void sub_002F28DD(void);
extern void sub_002F293D(void);
extern void sub_002F293F(void);
extern void sub_002F29C5(void);
extern void sub_002F29CB(void);
extern void sub_002F29EC(void);
extern void sub_002F35AD(void);
extern void sub_002F55EC(void);
extern void sub_002F56F8(void);
extern void sub_002F626D(void);
extern void sub_002F665C(void);
extern void sub_002F6687(void);
extern void sub_002F66A7(void);
extern void sub_002F6CEF(void);
extern void sub_002F703C(void);
extern void sub_002F70D7(void);
extern void sub_002F7129(void);
extern void sub_002F715F(void);
extern void sub_002F744A(void);
extern void sub_002F744B(void);
extern void sub_002F7481(void);
extern void sub_002F78FD(void);
extern void sub_002F97B4(void);
extern void sub_002F97B7(void);
extern void sub_002FB3D9(void);
extern void sub_002FB6E5(void);
extern void sub_002FB71E(void);
extern void sub_002FB9E6(void);
extern void sub_002FBA95(void);
extern void sub_002FBBEE(void);
extern void sub_002FBC21(void);
extern void sub_002FBE57(void);
extern void sub_002FC3D6(void);
extern void sub_002FD866(void);
extern void sub_002FDDA3(void);
extern void sub_002FE0FD(void);
extern void sub_002FE2AD(void);
extern void sub_002FEBD2(void);
extern void sub_002FEC48(void);
extern void sub_002FF055(void);
extern void sub_002FF058(void);
extern void sub_002FF2C8(void);
extern void sub_002FF373(void);
extern void sub_002FF376(void);
extern void sub_002FF5A8(void);
extern void sub_002FF814(void);
extern void sub_002FFB60(void);
extern void sub_002FFD9D(void);
extern void sub_002FFFBA(void);
extern void sub_0030015E(void);
extern void sub_00300920(void);
extern void sub_00300932(void);
extern void sub_00304010(void);
extern void sub_0030473B(void);
extern void sub_00307010(void);
extern void sub_0030765F(void);
extern void sub_00307A9D(void);
extern void sub_0030801A(void);
extern void sub_00308489(void);
extern void sub_00308820(void);
extern void sub_003093CD(void);
extern void sub_00313748(void);
extern void sub_00314383(void);
extern void sub_00314C10(void);
extern void sub_00317EBD(void);
extern void sub_003180B2(void);
extern void sub_003189F4(void);
extern void sub_00319859(void);
extern void sub_0031A2ED(void);
extern void sub_0031AF26(void);
extern void sub_0031D389(void);
extern void sub_0031D9AC(void);
extern void sub_0031EBAA(void);
extern void sub_0031F72A(void);
extern void sub_00322220(void);
extern void sub_003222CB(void);
extern void sub_00322530(void);
extern void sub_00323AFD(void);
extern void sub_003244BC(void);
extern void sub_00325A05(void);
extern void sub_00325AAA(void);
extern void sub_00325C0F(void);
extern void sub_00325C9E(void);
extern void sub_00326448(void);
extern void sub_00326650(void);
extern void sub_003266F7(void);
extern void sub_00328018(void);
extern void sub_003287F5(void);
extern void sub_00328864(void);
extern void sub_00328B40(void);
extern void sub_00328BAC(void);
extern void sub_0032A2E2(void);
extern void sub_0032A8D3(void);
extern void sub_0032A900(void);
extern void sub_0032C633(void);
extern void sub_0032C661(void);
extern void sub_0032C690(void);
extern void sub_0032C7E0(void);
extern void sub_0032C816(void);
extern void sub_0033A822(void);
extern void sub_0033D4EF(void);
extern void sub_0033D75D(void);
extern void sub_0033F381(void);
extern void sub_0033F6B0(void);
extern void sub_00340CDE(void);
extern void sub_00340D86(void);
extern void sub_00341137(void);
extern void sub_003412E1(void);
extern void sub_003412EA(void);
extern void sub_00341438(void);
extern void sub_00341699(void);
extern void sub_00341740(void);
extern void sub_00341935(void);
extern void sub_00341936(void);
extern void sub_00341A2C(void);
extern void sub_003421E8(void);
extern void sub_00342384(void);
extern void sub_003424E5(void);
extern void sub_003426D6(void);
extern void sub_00342708(void);
extern void sub_0034299D(void);
extern void sub_00343569(void);
extern void sub_0034360D(void);
extern void sub_00343862(void);
extern void sub_00343AD0(void);
extern void sub_00343D1C(void);
extern void sub_00343E62(void);
extern void sub_00343E65(void);
extern void sub_00343FE1(void);
extern void sub_00344878(void);
extern void sub_0034558A(void);
extern void sub_00345DC0(void);
extern void sub_00345E24(void);
extern void sub_00346743(void);
extern void sub_00347E1F(void);
extern void sub_00347E6A(void);
extern void sub_00348673(void);
extern void sub_003486B0(void);
extern void sub_003489E3(void);
extern void sub_00348A7E(void);
extern void sub_00348AE1(void);
extern void sub_00348AF2(void);
extern void sub_00348C8A(void);
extern void sub_00348CEC(void);
extern void sub_00349183(void);
extern void sub_0034921B(void);
extern void sub_003494FF(void);
extern void sub_00349F24(void);
extern void sub_00349FAB(void);
extern void sub_0034A3F1(void);
extern void sub_0034A57D(void);
extern void sub_0034A627(void);
extern void sub_0034A66F(void);
extern void sub_0034A744(void);
extern void sub_0034A9C3(void);
extern void sub_0034AA86(void);
extern void sub_0034AF6C(void);
extern void sub_0034B6B7(void);
extern void sub_0034BB3A(void);
extern void sub_0034BF1B(void);
extern void sub_0034C32B(void);
extern void sub_0034C59D(void);
extern void sub_0034C6C0(void);
extern void sub_0034C79E(void);
extern void sub_0034C8A7(void);
extern void sub_0034CC9C(void);
extern void sub_0034CC9F(void);
extern void sub_0034CCB5(void);
extern void sub_0034CD6F(void);
extern void sub_0034D06A(void);
extern void sub_0034D189(void);
extern void sub_0034D453(void);
extern void sub_0034D9A8(void);
extern void sub_0034DD08(void);
extern void sub_003556E0(void);
extern void sub_0035ADA0(void);
extern void sub_0035ADB0(void);
extern void sub_00361720(void);
extern void sub_00361EF0(void);
extern void sub_00368D90(void);
extern void sub_00368E40(void);
extern void sub_00368EA0(void);
extern void sub_0036F330(void);
extern void sub_0036F348(void);
extern void sub_0036F350(void);
extern void sub_0036F352(void);
extern void sub_0036F535(void);
extern void sub_0036F5F2(void);
extern void sub_0036F6DE(void);
extern void sub_0036F70F(void);
extern void sub_0036F73D(void);
extern void sub_0036F7A8(void);
extern void sub_0036F7C9(void);
extern void sub_0036F7D2(void);
extern void sub_0036F7DA(void);
extern void sub_00370666(void);
extern void sub_0037272B(void);
extern void sub_00396040(void);
extern void sub_00396060(void);
extern void sub_003971C7(void);
extern void sub_00397247(void);
extern void sub_0039724B(void);
extern void sub_0039737D(void);
extern void sub_0039737E(void);
extern void sub_0039737F(void);
extern void sub_00397386(void);
extern void sub_00397400(void);
extern void sub_00397480(void);
extern void sub_00397481(void);
extern void sub_00397556(void);
extern void sub_00397598(void);
extern void sub_00397618(void);
extern void sub_00397732(void);

/* Video playback shim overrides - Phase 1: stub to return success immediately */
extern void sub_00340FEB(void);
extern void sub_003432A8(void);
extern void sub_003464F1(void);
extern void sub_003467B6(void);
extern void sub_003467F2(void);

/* Network fallback override - stub to prevent blocking */
extern void sub_00345AB0(void);

recomp_func_t recomp_lookup_manual(uint32_t xbox_va)
{
    if (xbox_va == 0x00011B35u) return sub_00011B35;
    if (xbox_va == 0x00011EC0u) return sub_00011EC0;
    if (xbox_va == 0x000147EDu) return sub_000147ED;
    if (xbox_va == 0x0001486Fu) return sub_0001486F;
    if (xbox_va == 0x000149DFu) return sub_000149DF;
    if (xbox_va == 0x00014C60u) return sub_00014C60;
    if (xbox_va == 0x00014CC0u) return sub_00014CC0;
    if (xbox_va == 0x00014DAFu) return sub_00014DAF;
    if (xbox_va == 0x00014E9Fu) return sub_00014E9F;
    if (xbox_va == 0x0001511Fu) return sub_0001511F;
    if (xbox_va == 0x0001518Du) return sub_0001518D;
    if (xbox_va == 0x000151EFu) return sub_000151EF;
    if (xbox_va == 0x000152AFu) return sub_000152AF;
    if (xbox_va == 0x0001531Fu) return sub_0001531F;
    if (xbox_va == 0x00015868u) return sub_00015868;
    if (xbox_va == 0x000158E8u) return sub_000158E8;
    if (xbox_va == 0x00015BAEu) return sub_00015BAE;
    if (xbox_va == 0x00015C36u) return sub_00015C36;
    if (xbox_va == 0x000162E0u) return sub_000162E0;
    if (xbox_va == 0x0001661Cu) return sub_0001661C;
    if (xbox_va == 0x000189B4u) return sub_000189B4;
    if (xbox_va == 0x000189F8u) return sub_000189F8;
    if (xbox_va == 0x00018CB5u) return sub_00018CB5;
    if (xbox_va == 0x00019880u) return sub_00019880;
    if (xbox_va == 0x0001990Au) return sub_0001990A;
    if (xbox_va == 0x00019C00u) return sub_00019C00;
    if (xbox_va == 0x0001C577u) return sub_0001C577;
    if (xbox_va == 0x0001D872u) return sub_0001D872;
    if (xbox_va == 0x0001D9E6u) return sub_0001D9E6;
    if (xbox_va == 0x0001E0CFu) return sub_0001E0CF;
    if (xbox_va == 0x0001E0EBu) return sub_0001E0EB;
    if (xbox_va == 0x0001EDCBu) return sub_0001EDCB;
    if (xbox_va == 0x0001F315u) return sub_0001F315;
    if (xbox_va == 0x0001F989u) return sub_0001F989;
    if (xbox_va == 0x0001FE5Eu) return sub_0001FE5E;
    if (xbox_va == 0x000203B1u) return sub_000203B1;
    if (xbox_va == 0x00020856u) return sub_00020856;
    if (xbox_va == 0x00021582u) return sub_00021582;
    if (xbox_va == 0x00021DD4u) return sub_00021DD4;
    if (xbox_va == 0x0002581Fu) return sub_0002581F;
    if (xbox_va == 0x0002668Cu) return sub_0002668C;
    if (xbox_va == 0x0002719Cu) return sub_0002719C;
    if (xbox_va == 0x00028525u) return sub_00028525;
    if (xbox_va == 0x00028814u) return sub_00028814;
    if (xbox_va == 0x000298FFu) return sub_000298FF;
    if (xbox_va == 0x0002B1E0u) return sub_0002B1E0;
    if (xbox_va == 0x00030B3Bu) return sub_00030B3B;
    if (xbox_va == 0x00034EE9u) return sub_00034EE9;
    if (xbox_va == 0x000362AFu) return sub_000362AF;
    if (xbox_va == 0x000366F4u) return sub_000366F4;
    if (xbox_va == 0x00036DCCu) return sub_00036DCC;
    if (xbox_va == 0x00038909u) return sub_00038909;
    if (xbox_va == 0x00038A9Du) return sub_00038A9D;
    if (xbox_va == 0x000390E9u) return sub_000390E9;
    if (xbox_va == 0x000390F5u) return sub_000390F5;
    if (xbox_va == 0x00039C1Fu) return sub_00039C1F;
    if (xbox_va == 0x0003C7A7u) return sub_0003C7A7;
    if (xbox_va == 0x0003E103u) return sub_0003E103;
    if (xbox_va == 0x0003F54Cu) return sub_0003F54C;
    if (xbox_va == 0x0003FACEu) return sub_0003FACE;
    if (xbox_va == 0x00040304u) return sub_00040304;
    if (xbox_va == 0x0004353Du) return sub_0004353D;
    if (xbox_va == 0x00047D19u) return sub_00047D19;
    if (xbox_va == 0x0004834Au) return sub_0004834A;
    if (xbox_va == 0x000488CDu) return sub_000488CD;
    if (xbox_va == 0x000491EAu) return sub_000491EA;
    if (xbox_va == 0x000493D4u) return sub_000493D4;
    if (xbox_va == 0x000498FDu) return sub_000498FD;
    if (xbox_va == 0x00049E19u) return sub_00049E19;
    if (xbox_va == 0x0004DB20u) return sub_0004DB20;
    if (xbox_va == 0x0004E2F6u) return sub_0004E2F6;
    if (xbox_va == 0x0004E660u) return sub_0004E660;
    if (xbox_va == 0x0004ED62u) return sub_0004ED62;
    if (xbox_va == 0x0004EE28u) return sub_0004EE28;
    if (xbox_va == 0x0004FCD7u) return sub_0004FCD7;
    if (xbox_va == 0x000501C9u) return sub_000501C9;
    if (xbox_va == 0x00050762u) return sub_00050762;
    if (xbox_va == 0x00050CF2u) return sub_00050CF2;
    if (xbox_va == 0x00051B50u) return sub_00051B50;
    if (xbox_va == 0x00051FE1u) return sub_00051FE1;
    if (xbox_va == 0x000527FFu) return sub_000527FF;
    if (xbox_va == 0x0005590Du) return sub_0005590D;
    if (xbox_va == 0x00055A0Fu) return sub_00055A0F;
    if (xbox_va == 0x0005616Cu) return sub_0005616C;
    if (xbox_va == 0x000573CDu) return sub_000573CD;
    if (xbox_va == 0x000578F0u) return sub_000578F0;
    if (xbox_va == 0x00058AACu) return sub_00058AAC;
    if (xbox_va == 0x0005A0C8u) return sub_0005A0C8;
    if (xbox_va == 0x0005A0D0u) return sub_0005A0D0;
    if (xbox_va == 0x0005B4FEu) return sub_0005B4FE;
    if (xbox_va == 0x0005B502u) return sub_0005B502;
    if (xbox_va == 0x0005C0B5u) return sub_0005C0B5;
    if (xbox_va == 0x0005C1DAu) return sub_0005C1DA;
    if (xbox_va == 0x0005C61Bu) return sub_0005C61B;
    if (xbox_va == 0x0005CE0Bu) return sub_0005CE0B;
    if (xbox_va == 0x0005F5FDu) return sub_0005F5FD;
    if (xbox_va == 0x0005F83Du) return sub_0005F83D;
    if (xbox_va == 0x0005F8DDu) return sub_0005F8DD;
    if (xbox_va == 0x00060239u) return sub_00060239;
    if (xbox_va == 0x000602EBu) return sub_000602EB;
    if (xbox_va == 0x000603EEu) return sub_000603EE;
    if (xbox_va == 0x000643BBu) return sub_000643BB;
    if (xbox_va == 0x0006702Du) return sub_0006702D;
    if (xbox_va == 0x0006727Eu) return sub_0006727E;
    if (xbox_va == 0x00067508u) return sub_00067508;
    if (xbox_va == 0x00067AA9u) return sub_00067AA9;
    if (xbox_va == 0x00067EA8u) return sub_00067EA8;
    if (xbox_va == 0x000681CDu) return sub_000681CD;
    if (xbox_va == 0x0006822Fu) return sub_0006822F;
    if (xbox_va == 0x00068325u) return sub_00068325;
    if (xbox_va == 0x000684D9u) return sub_000684D9;
    if (xbox_va == 0x0006882Fu) return sub_0006882F;
    if (xbox_va == 0x0006891Cu) return sub_0006891C;
    if (xbox_va == 0x0006A07Bu) return sub_0006A07B;
    if (xbox_va == 0x0006A0BCu) return sub_0006A0BC;
    if (xbox_va == 0x0006A2D9u) return sub_0006A2D9;
    if (xbox_va == 0x0006AAB0u) return sub_0006AAB0;
    if (xbox_va == 0x0006C7BDu) return sub_0006C7BD;
    if (xbox_va == 0x0006CE84u) return sub_0006CE84;
    if (xbox_va == 0x0006DC4Fu) return sub_0006DC4F;
    if (xbox_va == 0x0006F6D1u) return sub_0006F6D1;
    if (xbox_va == 0x000707C2u) return sub_000707C2;
    if (xbox_va == 0x000707CFu) return sub_000707CF;
    if (xbox_va == 0x00070C20u) return sub_00070C20;
    if (xbox_va == 0x00070CFBu) return sub_00070CFB;
    if (xbox_va == 0x00071F12u) return sub_00071F12;
    if (xbox_va == 0x00075F1Bu) return sub_00075F1B;
    if (xbox_va == 0x000763FCu) return sub_000763FC;
    if (xbox_va == 0x00076E07u) return sub_00076E07;
    if (xbox_va == 0x00077719u) return sub_00077719;
    if (xbox_va == 0x00077916u) return sub_00077916;
    if (xbox_va == 0x0007816Fu) return sub_0007816F;
    if (xbox_va == 0x0007922Du) return sub_0007922D;
    if (xbox_va == 0x000795E8u) return sub_000795E8;
    if (xbox_va == 0x0007B724u) return sub_0007B724;
    if (xbox_va == 0x0007B774u) return sub_0007B774;
    if (xbox_va == 0x0007DDF8u) return sub_0007DDF8;
    if (xbox_va == 0x0007E1C6u) return sub_0007E1C6;
    if (xbox_va == 0x0007FC15u) return sub_0007FC15;
    if (xbox_va == 0x00082854u) return sub_00082854;
    if (xbox_va == 0x000828CBu) return sub_000828CB;
    if (xbox_va == 0x00082BB1u) return sub_00082BB1;
    if (xbox_va == 0x00082BB3u) return sub_00082BB3;
    if (xbox_va == 0x0008324Du) return sub_0008324D;
    if (xbox_va == 0x0008395Du) return sub_0008395D;
    if (xbox_va == 0x00083BA9u) return sub_00083BA9;
    if (xbox_va == 0x00083D52u) return sub_00083D52;
    if (xbox_va == 0x00083F1Cu) return sub_00083F1C;
    if (xbox_va == 0x00084488u) return sub_00084488;
    if (xbox_va == 0x00086727u) return sub_00086727;
    if (xbox_va == 0x00086B27u) return sub_00086B27;
    if (xbox_va == 0x00086D2Eu) return sub_00086D2E;
    if (xbox_va == 0x000882BEu) return sub_000882BE;
    if (xbox_va == 0x00088942u) return sub_00088942;
    if (xbox_va == 0x00088D13u) return sub_00088D13;
    if (xbox_va == 0x00088E44u) return sub_00088E44;
    if (xbox_va == 0x00088ED5u) return sub_00088ED5;
    if (xbox_va == 0x00089B5Du) return sub_00089B5D;
    if (xbox_va == 0x00089C8Fu) return sub_00089C8F;
    if (xbox_va == 0x00089D55u) return sub_00089D55;
    if (xbox_va == 0x00089E20u) return sub_00089E20;
    if (xbox_va == 0x0008CBD8u) return sub_0008CBD8;
    if (xbox_va == 0x0008D8D0u) return sub_0008D8D0;
    if (xbox_va == 0x0008DA4Eu) return sub_0008DA4E;
    if (xbox_va == 0x0008DBBDu) return sub_0008DBBD;
    if (xbox_va == 0x0008EFADu) return sub_0008EFAD;
    if (xbox_va == 0x0008F2CFu) return sub_0008F2CF;
    if (xbox_va == 0x00090BB9u) return sub_00090BB9;
    if (xbox_va == 0x0009175Du) return sub_0009175D;
    if (xbox_va == 0x00091EABu) return sub_00091EAB;
    if (xbox_va == 0x0009329Du) return sub_0009329D;
    if (xbox_va == 0x00093646u) return sub_00093646;
    if (xbox_va == 0x00093ED7u) return sub_00093ED7;
    if (xbox_va == 0x00094C26u) return sub_00094C26;
    if (xbox_va == 0x0009506Fu) return sub_0009506F;
    if (xbox_va == 0x0009515Du) return sub_0009515D;
    if (xbox_va == 0x00099C70u) return sub_00099C70;
    if (xbox_va == 0x00099CDEu) return sub_00099CDE;
    if (xbox_va == 0x0009C9E4u) return sub_0009C9E4;
    if (xbox_va == 0x0009E60Fu) return sub_0009E60F;
    if (xbox_va == 0x0009F092u) return sub_0009F092;
    if (xbox_va == 0x000A089Fu) return sub_000A089F;
    if (xbox_va == 0x000A108Du) return sub_000A108D;
    if (xbox_va == 0x000A1A40u) return sub_000A1A40;
    if (xbox_va == 0x000A1C00u) return sub_000A1C00;
    if (xbox_va == 0x000A21C3u) return sub_000A21C3;
    if (xbox_va == 0x000A2A93u) return sub_000A2A93;
    if (xbox_va == 0x000A3D3Bu) return sub_000A3D3B;
    if (xbox_va == 0x000A3E3Fu) return sub_000A3E3F;
    if (xbox_va == 0x000A4714u) return sub_000A4714;
    if (xbox_va == 0x000A589Bu) return sub_000A589B;
    if (xbox_va == 0x000A6672u) return sub_000A6672;
    if (xbox_va == 0x000A6675u) return sub_000A6675;
    if (xbox_va == 0x000A9F3Fu) return sub_000A9F3F;
    if (xbox_va == 0x000AC856u) return sub_000AC856;
    if (xbox_va == 0x000AD23Du) return sub_000AD23D;
    if (xbox_va == 0x000ADD0Bu) return sub_000ADD0B;
    if (xbox_va == 0x000B10AFu) return sub_000B10AF;
    if (xbox_va == 0x000B1A9Du) return sub_000B1A9D;
    if (xbox_va == 0x000B1AFDu) return sub_000B1AFD;
    if (xbox_va == 0x000B1FD0u) return sub_000B1FD0;
    if (xbox_va == 0x000B2ECFu) return sub_000B2ECF;
    if (xbox_va == 0x000B2F7Cu) return sub_000B2F7C;
    if (xbox_va == 0x000B3D8Fu) return sub_000B3D8F;
    if (xbox_va == 0x000B3DFFu) return sub_000B3DFF;
    if (xbox_va == 0x000B416Du) return sub_000B416D;
    if (xbox_va == 0x000B46E2u) return sub_000B46E2;
    if (xbox_va == 0x000B55A5u) return sub_000B55A5;
    if (xbox_va == 0x000B5A85u) return sub_000B5A85;
    if (xbox_va == 0x000B5A90u) return sub_000B5A90;
    if (xbox_va == 0x000B5C9Cu) return sub_000B5C9C;
    if (xbox_va == 0x000B636Eu) return sub_000B636E;
    if (xbox_va == 0x000B7220u) return sub_000B7220;
    if (xbox_va == 0x000B757Fu) return sub_000B757F;
    if (xbox_va == 0x000B7851u) return sub_000B7851;
    if (xbox_va == 0x000B7D4Du) return sub_000B7D4D;
    if (xbox_va == 0x000B7E39u) return sub_000B7E39;
    if (xbox_va == 0x000B8C4Au) return sub_000B8C4A;
    if (xbox_va == 0x000B9182u) return sub_000B9182;
    if (xbox_va == 0x000BA4E2u) return sub_000BA4E2;
    if (xbox_va == 0x000BA92Au) return sub_000BA92A;
    if (xbox_va == 0x000BB11Du) return sub_000BB11D;
    if (xbox_va == 0x000BB1DDu) return sub_000BB1DD;
    if (xbox_va == 0x000BCEA9u) return sub_000BCEA9;
    if (xbox_va == 0x000BD66Fu) return sub_000BD66F;
    if (xbox_va == 0x000BD73Fu) return sub_000BD73F;
    if (xbox_va == 0x000BD7AFu) return sub_000BD7AF;
    if (xbox_va == 0x000BD99Cu) return sub_000BD99C;
    if (xbox_va == 0x000BED70u) return sub_000BED70;
    if (xbox_va == 0x000BF2F3u) return sub_000BF2F3;
    if (xbox_va == 0x000BF3CFu) return sub_000BF3CF;
    if (xbox_va == 0x000BFDFFu) return sub_000BFDFF;
    if (xbox_va == 0x000C0801u) return sub_000C0801;
    if (xbox_va == 0x000C0815u) return sub_000C0815;
    if (xbox_va == 0x000C08D3u) return sub_000C08D3;
    if (xbox_va == 0x000C08E7u) return sub_000C08E7;
    if (xbox_va == 0x000C0B60u) return sub_000C0B60;
    if (xbox_va == 0x000C1BE3u) return sub_000C1BE3;
    if (xbox_va == 0x000C3E85u) return sub_000C3E85;
    if (xbox_va == 0x000C859Eu) return sub_000C859E;
    if (xbox_va == 0x000C9FCDu) return sub_000C9FCD;
    if (xbox_va == 0x000CA17Du) return sub_000CA17D;
    if (xbox_va == 0x000CA1DFu) return sub_000CA1DF;
    if (xbox_va == 0x000CA4BFu) return sub_000CA4BF;
    if (xbox_va == 0x000CA52Du) return sub_000CA52D;
    if (xbox_va == 0x000CA58Du) return sub_000CA58D;
    if (xbox_va == 0x000CA5EFu) return sub_000CA5EF;
    if (xbox_va == 0x000CA65Fu) return sub_000CA65F;
    if (xbox_va == 0x000CA6CDu) return sub_000CA6CD;
    if (xbox_va == 0x000CA75Fu) return sub_000CA75F;
    if (xbox_va == 0x000CB39Cu) return sub_000CB39C;
    if (xbox_va == 0x000CB439u) return sub_000CB439;
    if (xbox_va == 0x000CB4FCu) return sub_000CB4FC;
    if (xbox_va == 0x000CFBCFu) return sub_000CFBCF;
    if (xbox_va == 0x000CFF7Cu) return sub_000CFF7C;
    if (xbox_va == 0x000D07A0u) return sub_000D07A0;
    if (xbox_va == 0x000D10A1u) return sub_000D10A1;
    if (xbox_va == 0x000D13BAu) return sub_000D13BA;
    if (xbox_va == 0x000D3DCDu) return sub_000D3DCD;
    if (xbox_va == 0x000D5642u) return sub_000D5642;
    if (xbox_va == 0x000D62C1u) return sub_000D62C1;
    if (xbox_va == 0x000D62F1u) return sub_000D62F1;
    if (xbox_va == 0x000D87E2u) return sub_000D87E2;
    if (xbox_va == 0x000DADBDu) return sub_000DADBD;
    if (xbox_va == 0x000DC125u) return sub_000DC125;
    if (xbox_va == 0x000DC2FEu) return sub_000DC2FE;
    if (xbox_va == 0x000DC300u) return sub_000DC300;
    if (xbox_va == 0x000DCCE6u) return sub_000DCCE6;
    if (xbox_va == 0x000E220Du) return sub_000E220D;
    if (xbox_va == 0x000E306Fu) return sub_000E306F;
    if (xbox_va == 0x000E30DFu) return sub_000E30DF;
    if (xbox_va == 0x000E3549u) return sub_000E3549;
    if (xbox_va == 0x000E3A10u) return sub_000E3A10;
    if (xbox_va == 0x000E7A2Du) return sub_000E7A2D;
    if (xbox_va == 0x000E98FDu) return sub_000E98FD;
    if (xbox_va == 0x000E9C74u) return sub_000E9C74;
    if (xbox_va == 0x000EA51Fu) return sub_000EA51F;
    if (xbox_va == 0x000EAA2Fu) return sub_000EAA2F;
    if (xbox_va == 0x000EB740u) return sub_000EB740;
    if (xbox_va == 0x000EB756u) return sub_000EB756;
    if (xbox_va == 0x000EB7B2u) return sub_000EB7B2;
    if (xbox_va == 0x000EBA2Cu) return sub_000EBA2C;
    if (xbox_va == 0x000EBD30u) return sub_000EBD30;
    if (xbox_va == 0x000EBD97u) return sub_000EBD97;
    if (xbox_va == 0x000EC6FEu) return sub_000EC6FE;
    if (xbox_va == 0x000ECBFBu) return sub_000ECBFB;
    if (xbox_va == 0x000F116Bu) return sub_000F116B;
    if (xbox_va == 0x000F241Bu) return sub_000F241B;
    if (xbox_va == 0x000F68EDu) return sub_000F68ED;
    if (xbox_va == 0x000F694Du) return sub_000F694D;
    if (xbox_va == 0x000F6B28u) return sub_000F6B28;
    if (xbox_va == 0x000F6BE5u) return sub_000F6BE5;
    if (xbox_va == 0x000F7BD6u) return sub_000F7BD6;
    if (xbox_va == 0x000F7D4Eu) return sub_000F7D4E;
    if (xbox_va == 0x000F7E6Du) return sub_000F7E6D;
    if (xbox_va == 0x000F7ECDu) return sub_000F7ECD;
    if (xbox_va == 0x000FCB1Fu) return sub_000FCB1F;
    if (xbox_va == 0x000FD42Cu) return sub_000FD42C;
    if (xbox_va == 0x000FD731u) return sub_000FD731;
    if (xbox_va == 0x000FD7BBu) return sub_000FD7BB;
    if (xbox_va == 0x000FE7E3u) return sub_000FE7E3;
    if (xbox_va == 0x000FEA30u) return sub_000FEA30;
    if (xbox_va == 0x000FF085u) return sub_000FF085;
    if (xbox_va == 0x000FF087u) return sub_000FF087;
    if (xbox_va == 0x0010168Du) return sub_0010168D;
    if (xbox_va == 0x0010262Cu) return sub_0010262C;
    if (xbox_va == 0x0010617Du) return sub_0010617D;
    if (xbox_va == 0x0010632Eu) return sub_0010632E;
    if (xbox_va == 0x00106DD0u) return sub_00106DD0;
    if (xbox_va == 0x001072FEu) return sub_001072FE;
    if (xbox_va == 0x00108486u) return sub_00108486;
    if (xbox_va == 0x0010901Fu) return sub_0010901F;
    if (xbox_va == 0x0010A30Bu) return sub_0010A30B;
    if (xbox_va == 0x0010CF2Eu) return sub_0010CF2E;
    if (xbox_va == 0x0010DA10u) return sub_0010DA10;
    if (xbox_va == 0x0010E75Eu) return sub_0010E75E;
    if (xbox_va == 0x0011316Fu) return sub_0011316F;
    if (xbox_va == 0x0011742Du) return sub_0011742D;
    if (xbox_va == 0x001183EDu) return sub_001183ED;
    if (xbox_va == 0x00118839u) return sub_00118839;
    if (xbox_va == 0x0011B58Fu) return sub_0011B58F;
    if (xbox_va == 0x0011B60Fu) return sub_0011B60F;
    if (xbox_va == 0x0011C44Bu) return sub_0011C44B;
    if (xbox_va == 0x0011C53Du) return sub_0011C53D;
    if (xbox_va == 0x0011E13Du) return sub_0011E13D;
    if (xbox_va == 0x0011FB69u) return sub_0011FB69;
    if (xbox_va == 0x00120070u) return sub_00120070;
    if (xbox_va == 0x0012176Du) return sub_0012176D;
    if (xbox_va == 0x0012183Du) return sub_0012183D;
    if (xbox_va == 0x00121E39u) return sub_00121E39;
    if (xbox_va == 0x00121EC9u) return sub_00121EC9;
    if (xbox_va == 0x001238DAu) return sub_001238DA;
    if (xbox_va == 0x00123B35u) return sub_00123B35;
    if (xbox_va == 0x0012410Du) return sub_0012410D;
    if (xbox_va == 0x00124399u) return sub_00124399;
    if (xbox_va == 0x001245B9u) return sub_001245B9;
    if (xbox_va == 0x001248BFu) return sub_001248BF;
    if (xbox_va == 0x00125050u) return sub_00125050;
    if (xbox_va == 0x00126754u) return sub_00126754;
    if (xbox_va == 0x00126834u) return sub_00126834;
    if (xbox_va == 0x00126944u) return sub_00126944;
    if (xbox_va == 0x00126A34u) return sub_00126A34;
    if (xbox_va == 0x001271EDu) return sub_001271ED;
    if (xbox_va == 0x00128204u) return sub_00128204;
    if (xbox_va == 0x00128394u) return sub_00128394;
    if (xbox_va == 0x001286C8u) return sub_001286C8;
    if (xbox_va == 0x001286E2u) return sub_001286E2;
    if (xbox_va == 0x0012A8F3u) return sub_0012A8F3;
    if (xbox_va == 0x0012DB5Fu) return sub_0012DB5F;
    if (xbox_va == 0x0012E44Du) return sub_0012E44D;
    if (xbox_va == 0x0012ECB0u) return sub_0012ECB0;
    if (xbox_va == 0x0012ECD5u) return sub_0012ECD5;
    if (xbox_va == 0x0012EE50u) return sub_0012EE50;
    if (xbox_va == 0x0012EE5Eu) return sub_0012EE5E;
    if (xbox_va == 0x0012F563u) return sub_0012F563;
    if (xbox_va == 0x0012F8F0u) return sub_0012F8F0;
    if (xbox_va == 0x001303AEu) return sub_001303AE;
    if (xbox_va == 0x00130BB7u) return sub_00130BB7;
    if (xbox_va == 0x00130BC0u) return sub_00130BC0;
    if (xbox_va == 0x00130BCEu) return sub_00130BCE;
    if (xbox_va == 0x001315B1u) return sub_001315B1;
    if (xbox_va == 0x001315BFu) return sub_001315BF;
    if (xbox_va == 0x00131892u) return sub_00131892;
    if (xbox_va == 0x001318A0u) return sub_001318A0;
    if (xbox_va == 0x00131B3Fu) return sub_00131B3F;
    if (xbox_va == 0x00132310u) return sub_00132310;
    if (xbox_va == 0x00132335u) return sub_00132335;
    if (xbox_va == 0x00132370u) return sub_00132370;
    if (xbox_va == 0x00132395u) return sub_00132395;
    if (xbox_va == 0x001323D0u) return sub_001323D0;
    if (xbox_va == 0x001323F5u) return sub_001323F5;
    if (xbox_va == 0x00134AFDu) return sub_00134AFD;
    if (xbox_va == 0x001352B9u) return sub_001352B9;
    if (xbox_va == 0x00135E8Fu) return sub_00135E8F;
    if (xbox_va == 0x00135F8Fu) return sub_00135F8F;
    if (xbox_va == 0x0013608Fu) return sub_0013608F;
    if (xbox_va == 0x0013618Fu) return sub_0013618F;
    if (xbox_va == 0x0013632Fu) return sub_0013632F;
    if (xbox_va == 0x001364EFu) return sub_001364EF;
    if (xbox_va == 0x00138A5Fu) return sub_00138A5F;
    if (xbox_va == 0x001392B1u) return sub_001392B1;
    if (xbox_va == 0x0013939Cu) return sub_0013939C;
    if (xbox_va == 0x00139D5Cu) return sub_00139D5C;
    if (xbox_va == 0x0013A238u) return sub_0013A238;
    if (xbox_va == 0x0013A23Du) return sub_0013A23D;
    if (xbox_va == 0x0013C94Du) return sub_0013C94D;
    if (xbox_va == 0x0013CC14u) return sub_0013CC14;
    if (xbox_va == 0x0013CCE0u) return sub_0013CCE0;
    if (xbox_va == 0x0013E974u) return sub_0013E974;
    if (xbox_va == 0x0013EECAu) return sub_0013EECA;
    if (xbox_va == 0x0013EFBAu) return sub_0013EFBA;
    if (xbox_va == 0x0014388Fu) return sub_0014388F;
    if (xbox_va == 0x001454BDu) return sub_001454BD;
    if (xbox_va == 0x00146BDFu) return sub_00146BDF;
    if (xbox_va == 0x00146CDFu) return sub_00146CDF;
    if (xbox_va == 0x00148797u) return sub_00148797;
    if (xbox_va == 0x001489BDu) return sub_001489BD;
    if (xbox_va == 0x0014992Fu) return sub_0014992F;
    if (xbox_va == 0x0014BF1Du) return sub_0014BF1D;
    if (xbox_va == 0x0014E98Fu) return sub_0014E98F;
    if (xbox_va == 0x0014EAFFu) return sub_0014EAFF;
    if (xbox_va == 0x0014EB7Fu) return sub_0014EB7F;
    if (xbox_va == 0x0014EBEFu) return sub_0014EBEF;
    if (xbox_va == 0x0014ECBDu) return sub_0014ECBD;
    if (xbox_va == 0x0014F05Cu) return sub_0014F05C;
    if (xbox_va == 0x0014F0ECu) return sub_0014F0EC;
    if (xbox_va == 0x0014F179u) return sub_0014F179;
    if (xbox_va == 0x0014F26Cu) return sub_0014F26C;
    if (xbox_va == 0x0014F31Cu) return sub_0014F31C;
    if (xbox_va == 0x00150D9Cu) return sub_00150D9C;
    if (xbox_va == 0x00152D60u) return sub_00152D60;
    if (xbox_va == 0x00155FABu) return sub_00155FAB;
    if (xbox_va == 0x00156012u) return sub_00156012;
    if (xbox_va == 0x00156070u) return sub_00156070;
    if (xbox_va == 0x001560DDu) return sub_001560DD;
    if (xbox_va == 0x001561BDu) return sub_001561BD;
    if (xbox_va == 0x001562DDu) return sub_001562DD;
    if (xbox_va == 0x001564C5u) return sub_001564C5;
    if (xbox_va == 0x0015729Du) return sub_0015729D;
    if (xbox_va == 0x001572A0u) return sub_001572A0;
    if (xbox_va == 0x001574BDu) return sub_001574BD;
    if (xbox_va == 0x00157547u) return sub_00157547;
    if (xbox_va == 0x00157550u) return sub_00157550;
    if (xbox_va == 0x0015777Du) return sub_0015777D;
    if (xbox_va == 0x00157930u) return sub_00157930;
    if (xbox_va == 0x00158972u) return sub_00158972;
    if (xbox_va == 0x00158A72u) return sub_00158A72;
    if (xbox_va == 0x0015A29Du) return sub_0015A29D;
    if (xbox_va == 0x0015B393u) return sub_0015B393;
    if (xbox_va == 0x00161D5Fu) return sub_00161D5F;
    if (xbox_va == 0x00162BF9u) return sub_00162BF9;
    if (xbox_va == 0x00163D7Cu) return sub_00163D7C;
    if (xbox_va == 0x00166330u) return sub_00166330;
    if (xbox_va == 0x0016639Du) return sub_0016639D;
    if (xbox_va == 0x00166EB2u) return sub_00166EB2;
    if (xbox_va == 0x0016817Cu) return sub_0016817C;
    if (xbox_va == 0x00168B18u) return sub_00168B18;
    if (xbox_va == 0x00169070u) return sub_00169070;
    if (xbox_va == 0x0016A405u) return sub_0016A405;
    if (xbox_va == 0x0016A470u) return sub_0016A470;
    if (xbox_va == 0x0016B944u) return sub_0016B944;
    if (xbox_va == 0x0016BF05u) return sub_0016BF05;
    if (xbox_va == 0x0016C019u) return sub_0016C019;
    if (xbox_va == 0x0016C52Fu) return sub_0016C52F;
    if (xbox_va == 0x0016CC46u) return sub_0016CC46;
    if (xbox_va == 0x0016F150u) return sub_0016F150;
    if (xbox_va == 0x0016FD69u) return sub_0016FD69;
    if (xbox_va == 0x001701F5u) return sub_001701F5;
    if (xbox_va == 0x00170989u) return sub_00170989;
    if (xbox_va == 0x00171CA3u) return sub_00171CA3;
    if (xbox_va == 0x001726DDu) return sub_001726DD;
    if (xbox_va == 0x00174E00u) return sub_00174E00;
    if (xbox_va == 0x00174E04u) return sub_00174E04;
    if (xbox_va == 0x00176BBDu) return sub_00176BBD;
    if (xbox_va == 0x00177A30u) return sub_00177A30;
    if (xbox_va == 0x00177DA9u) return sub_00177DA9;
    if (xbox_va == 0x00177E09u) return sub_00177E09;
    if (xbox_va == 0x00177E62u) return sub_00177E62;
    if (xbox_va == 0x00178082u) return sub_00178082;
    if (xbox_va == 0x001780E0u) return sub_001780E0;
    if (xbox_va == 0x0017817Bu) return sub_0017817B;
    if (xbox_va == 0x00178230u) return sub_00178230;
    if (xbox_va == 0x0017859Du) return sub_0017859D;
    if (xbox_va == 0x001789EDu) return sub_001789ED;
    if (xbox_va == 0x00178A06u) return sub_00178A06;
    if (xbox_va == 0x00178CB8u) return sub_00178CB8;
    if (xbox_va == 0x00178F6Eu) return sub_00178F6E;
    if (xbox_va == 0x0017A53Fu) return sub_0017A53F;
    if (xbox_va == 0x0017A634u) return sub_0017A634;
    if (xbox_va == 0x0017D72Fu) return sub_0017D72F;
    if (xbox_va == 0x0017E5EDu) return sub_0017E5ED;
    if (xbox_va == 0x0017EEDBu) return sub_0017EEDB;
    if (xbox_va == 0x00182DCDu) return sub_00182DCD;
    if (xbox_va == 0x00182FFFu) return sub_00182FFF;
    if (xbox_va == 0x00183379u) return sub_00183379;
    if (xbox_va == 0x001860F6u) return sub_001860F6;
    if (xbox_va == 0x00186B95u) return sub_00186B95;
    if (xbox_va == 0x00186F1Au) return sub_00186F1A;
    if (xbox_va == 0x001874C0u) return sub_001874C0;
    if (xbox_va == 0x00188D8Du) return sub_00188D8D;
    if (xbox_va == 0x0018A18Fu) return sub_0018A18F;
    if (xbox_va == 0x0018A550u) return sub_0018A550;
    if (xbox_va == 0x0018AA5Fu) return sub_0018AA5F;
    if (xbox_va == 0x0018BA51u) return sub_0018BA51;
    if (xbox_va == 0x0018C82Du) return sub_0018C82D;
    if (xbox_va == 0x0018E9D0u) return sub_0018E9D0;
    if (xbox_va == 0x0018F3E3u) return sub_0018F3E3;
    if (xbox_va == 0x0018FC24u) return sub_0018FC24;
    if (xbox_va == 0x001900A0u) return sub_001900A0;
    if (xbox_va == 0x001917FAu) return sub_001917FA;
    if (xbox_va == 0x00191B19u) return sub_00191B19;
    if (xbox_va == 0x00191D32u) return sub_00191D32;
    if (xbox_va == 0x00192952u) return sub_00192952;
    if (xbox_va == 0x00192956u) return sub_00192956;
    if (xbox_va == 0x00193850u) return sub_00193850;
    if (xbox_va == 0x00194783u) return sub_00194783;
    if (xbox_va == 0x001967B1u) return sub_001967B1;
    if (xbox_va == 0x001970DDu) return sub_001970DD;
    if (xbox_va == 0x00198725u) return sub_00198725;
    if (xbox_va == 0x00198978u) return sub_00198978;
    if (xbox_va == 0x00198EADu) return sub_00198EAD;
    if (xbox_va == 0x0019961Au) return sub_0019961A;
    if (xbox_va == 0x0019AD3Cu) return sub_0019AD3C;
    if (xbox_va == 0x0019CC8Du) return sub_0019CC8D;
    if (xbox_va == 0x0019D07Cu) return sub_0019D07C;
    if (xbox_va == 0x0019D5DFu) return sub_0019D5DF;
    if (xbox_va == 0x0019E00Bu) return sub_0019E00B;
    if (xbox_va == 0x0019E082u) return sub_0019E082;
    if (xbox_va == 0x0019E096u) return sub_0019E096;
    if (xbox_va == 0x0019E103u) return sub_0019E103;
    if (xbox_va == 0x0019E17Au) return sub_0019E17A;
    if (xbox_va == 0x0019E49Eu) return sub_0019E49E;
    if (xbox_va == 0x0019E4D0u) return sub_0019E4D0;
    if (xbox_va == 0x0019E7AEu) return sub_0019E7AE;
    if (xbox_va == 0x0019E99Fu) return sub_0019E99F;
    if (xbox_va == 0x0019EB99u) return sub_0019EB99;
    if (xbox_va == 0x0019EE3Du) return sub_0019EE3D;
    if (xbox_va == 0x0019EF2Du) return sub_0019EF2D;
    if (xbox_va == 0x0019EF88u) return sub_0019EF88;
    if (xbox_va == 0x0019F0E3u) return sub_0019F0E3;
    if (xbox_va == 0x0019F193u) return sub_0019F193;
    if (xbox_va == 0x0019F306u) return sub_0019F306;
    if (xbox_va == 0x0019F39Fu) return sub_0019F39F;
    if (xbox_va == 0x0019F41Du) return sub_0019F41D;
    if (xbox_va == 0x0019F7A2u) return sub_0019F7A2;
    if (xbox_va == 0x0019F7A4u) return sub_0019F7A4;
    if (xbox_va == 0x0019F7DCu) return sub_0019F7DC;
    if (xbox_va == 0x0019FE96u) return sub_0019FE96;
    if (xbox_va == 0x001A0163u) return sub_001A0163;
    if (xbox_va == 0x001A04C3u) return sub_001A04C3;
    if (xbox_va == 0x001A1451u) return sub_001A1451;
    if (xbox_va == 0x001A2466u) return sub_001A2466;
    if (xbox_va == 0x001A2878u) return sub_001A2878;
    if (xbox_va == 0x001A297Bu) return sub_001A297B;
    if (xbox_va == 0x001A2D64u) return sub_001A2D64;
    if (xbox_va == 0x001A3325u) return sub_001A3325;
    if (xbox_va == 0x001A3B58u) return sub_001A3B58;
    if (xbox_va == 0x001A3BE7u) return sub_001A3BE7;
    if (xbox_va == 0x001A44CAu) return sub_001A44CA;
    if (xbox_va == 0x001A62D5u) return sub_001A62D5;
    if (xbox_va == 0x001A6345u) return sub_001A6345;
    if (xbox_va == 0x001A7600u) return sub_001A7600;
    if (xbox_va == 0x001AC5A2u) return sub_001AC5A2;
    if (xbox_va == 0x001AC5ACu) return sub_001AC5AC;
    if (xbox_va == 0x001AC5B2u) return sub_001AC5B2;
    if (xbox_va == 0x001ACCC6u) return sub_001ACCC6;
    if (xbox_va == 0x001ACEACu) return sub_001ACEAC;
    if (xbox_va == 0x001AE58Cu) return sub_001AE58C;
    if (xbox_va == 0x001AECA8u) return sub_001AECA8;
    if (xbox_va == 0x001B00ECu) return sub_001B00EC;
    if (xbox_va == 0x001B2620u) return sub_001B2620;
    if (xbox_va == 0x001B5CB9u) return sub_001B5CB9;
    if (xbox_va == 0x001B5D55u) return sub_001B5D55;
    if (xbox_va == 0x001B5E59u) return sub_001B5E59;
    if (xbox_va == 0x001B5EF5u) return sub_001B5EF5;
    if (xbox_va == 0x001B635Fu) return sub_001B635F;
    if (xbox_va == 0x001B673Fu) return sub_001B673F;
    if (xbox_va == 0x001B8480u) return sub_001B8480;
    if (xbox_va == 0x001BBF30u) return sub_001BBF30;
    if (xbox_va == 0x001BC091u) return sub_001BC091;
    if (xbox_va == 0x001BC4CFu) return sub_001BC4CF;
    if (xbox_va == 0x001BCB96u) return sub_001BCB96;
    if (xbox_va == 0x001BCD10u) return sub_001BCD10;
    if (xbox_va == 0x001BCD42u) return sub_001BCD42;
    if (xbox_va == 0x001BDDD9u) return sub_001BDDD9;
    if (xbox_va == 0x001C09F0u) return sub_001C09F0;
    if (xbox_va == 0x001C0BF6u) return sub_001C0BF6;
    if (xbox_va == 0x001C0C36u) return sub_001C0C36;
    if (xbox_va == 0x001C1190u) return sub_001C1190;
    if (xbox_va == 0x001C1301u) return sub_001C1301;
    if (xbox_va == 0x001C1EC0u) return sub_001C1EC0;
    if (xbox_va == 0x001C246Cu) return sub_001C246C;
    if (xbox_va == 0x001C9637u) return sub_001C9637;
    if (xbox_va == 0x001CCCFDu) return sub_001CCCFD;
    if (xbox_va == 0x001CD301u) return sub_001CD301;
    if (xbox_va == 0x001CED3Du) return sub_001CED3D;
    if (xbox_va == 0x001CEDB5u) return sub_001CEDB5;
    if (xbox_va == 0x001D0650u) return sub_001D0650;
    if (xbox_va == 0x001D0676u) return sub_001D0676;
    if (xbox_va == 0x001D2E0Eu) return sub_001D2E0E;
    if (xbox_va == 0x001D350Eu) return sub_001D350E;
    if (xbox_va == 0x001D3615u) return sub_001D3615;
    if (xbox_va == 0x001D3798u) return sub_001D3798;
    if (xbox_va == 0x001D37E6u) return sub_001D37E6;
    if (xbox_va == 0x001D3BA5u) return sub_001D3BA5;
    if (xbox_va == 0x001DACDFu) return sub_001DACDF;
    if (xbox_va == 0x001DC79Du) return sub_001DC79D;
    if (xbox_va == 0x001DD74Au) return sub_001DD74A;
    if (xbox_va == 0x001E1F58u) return sub_001E1F58;
    if (xbox_va == 0x001E26F9u) return sub_001E26F9;
    if (xbox_va == 0x001E4F9Au) return sub_001E4F9A;
    if (xbox_va == 0x001E55A2u) return sub_001E55A2;
    if (xbox_va == 0x001E5EC0u) return sub_001E5EC0;
    if (xbox_va == 0x001E7F50u) return sub_001E7F50;
    if (xbox_va == 0x001E8682u) return sub_001E8682;
    if (xbox_va == 0x001EC0D9u) return sub_001EC0D9;
    if (xbox_va == 0x001EC0E4u) return sub_001EC0E4;
    if (xbox_va == 0x001EC5E0u) return sub_001EC5E0;
    if (xbox_va == 0x001EC750u) return sub_001EC750;
    if (xbox_va == 0x001ECA9Du) return sub_001ECA9D;
    if (xbox_va == 0x001ED185u) return sub_001ED185;
    if (xbox_va == 0x001ED2E7u) return sub_001ED2E7;
    if (xbox_va == 0x001EF6F8u) return sub_001EF6F8;
    if (xbox_va == 0x001F51CCu) return sub_001F51CC;
    if (xbox_va == 0x001F51CFu) return sub_001F51CF;
    if (xbox_va == 0x001F5633u) return sub_001F5633;
    if (xbox_va == 0x001F56A8u) return sub_001F56A8;
    if (xbox_va == 0x001F5804u) return sub_001F5804;
    if (xbox_va == 0x001FBD3Cu) return sub_001FBD3C;
    if (xbox_va == 0x001FC17Bu) return sub_001FC17B;
    if (xbox_va == 0x001FC457u) return sub_001FC457;
    if (xbox_va == 0x001FCE30u) return sub_001FCE30;
    if (xbox_va == 0x001FD034u) return sub_001FD034;
    if (xbox_va == 0x001FE8A0u) return sub_001FE8A0;
    if (xbox_va == 0x001FE8E7u) return sub_001FE8E7;
    if (xbox_va == 0x002025B2u) return sub_002025B2;
    if (xbox_va == 0x002025DAu) return sub_002025DA;
    if (xbox_va == 0x00202890u) return sub_00202890;
    if (xbox_va == 0x002028B8u) return sub_002028B8;
    if (xbox_va == 0x00202A31u) return sub_00202A31;
    if (xbox_va == 0x00202A59u) return sub_00202A59;
    if (xbox_va == 0x00202D89u) return sub_00202D89;
    if (xbox_va == 0x00204967u) return sub_00204967;
    if (xbox_va == 0x00204999u) return sub_00204999;
    if (xbox_va == 0x00204DD7u) return sub_00204DD7;
    if (xbox_va == 0x00204DE2u) return sub_00204DE2;
    if (xbox_va == 0x00206E2Eu) return sub_00206E2E;
    if (xbox_va == 0x0020E493u) return sub_0020E493;
    if (xbox_va == 0x002126F6u) return sub_002126F6;
    if (xbox_va == 0x00215B7Du) return sub_00215B7D;
    if (xbox_va == 0x0021624Cu) return sub_0021624C;
    if (xbox_va == 0x0021644Bu) return sub_0021644B;
    if (xbox_va == 0x0021679Fu) return sub_0021679F;
    if (xbox_va == 0x00218360u) return sub_00218360;
    if (xbox_va == 0x0021837Au) return sub_0021837A;
    if (xbox_va == 0x0021A696u) return sub_0021A696;
    if (xbox_va == 0x0021A9D4u) return sub_0021A9D4;
    if (xbox_va == 0x0021AB18u) return sub_0021AB18;
    if (xbox_va == 0x0021B011u) return sub_0021B011;
    if (xbox_va == 0x0021B16Bu) return sub_0021B16B;
    if (xbox_va == 0x0021D19Bu) return sub_0021D19B;
    if (xbox_va == 0x0021D5BFu) return sub_0021D5BF;
    if (xbox_va == 0x0021D893u) return sub_0021D893;
    if (xbox_va == 0x0021E370u) return sub_0021E370;
    if (xbox_va == 0x0021E3A5u) return sub_0021E3A5;
    if (xbox_va == 0x0021E595u) return sub_0021E595;
    if (xbox_va == 0x0021EA41u) return sub_0021EA41;
    if (xbox_va == 0x0021F41Fu) return sub_0021F41F;
    if (xbox_va == 0x002242D1u) return sub_002242D1;
    if (xbox_va == 0x00225EF1u) return sub_00225EF1;
    if (xbox_va == 0x00227F50u) return sub_00227F50;
    if (xbox_va == 0x0022CEC5u) return sub_0022CEC5;
    if (xbox_va == 0x0022D08Fu) return sub_0022D08F;
    if (xbox_va == 0x0022DA30u) return sub_0022DA30;
    if (xbox_va == 0x0022DA9Cu) return sub_0022DA9C;
    if (xbox_va == 0x0022EBB0u) return sub_0022EBB0;
    if (xbox_va == 0x0022F70Fu) return sub_0022F70F;
    if (xbox_va == 0x0023037Du) return sub_0023037D;
    if (xbox_va == 0x002303B0u) return sub_002303B0;
    if (xbox_va == 0x0023065Du) return sub_0023065D;
    if (xbox_va == 0x00231C6Au) return sub_00231C6A;
    if (xbox_va == 0x002337E3u) return sub_002337E3;
    if (xbox_va == 0x00235002u) return sub_00235002;
    if (xbox_va == 0x0023575Cu) return sub_0023575C;
    if (xbox_va == 0x00235DD3u) return sub_00235DD3;
    if (xbox_va == 0x002366BCu) return sub_002366BC;
    if (xbox_va == 0x002370B0u) return sub_002370B0;
    if (xbox_va == 0x00237F7Cu) return sub_00237F7C;
    if (xbox_va == 0x002381ADu) return sub_002381AD;
    if (xbox_va == 0x00238309u) return sub_00238309;
    if (xbox_va == 0x00238887u) return sub_00238887;
    if (xbox_va == 0x0023A7B0u) return sub_0023A7B0;
    if (xbox_va == 0x0023B9FFu) return sub_0023B9FF;
    if (xbox_va == 0x0023BF80u) return sub_0023BF80;
    if (xbox_va == 0x0023C170u) return sub_0023C170;
    if (xbox_va == 0x0023C65Fu) return sub_0023C65F;
    if (xbox_va == 0x0023E000u) return sub_0023E000;
    if (xbox_va == 0x0023E7E0u) return sub_0023E7E0;
    if (xbox_va == 0x0023F8C0u) return sub_0023F8C0;
    if (xbox_va == 0x002446E6u) return sub_002446E6;
    if (xbox_va == 0x00246030u) return sub_00246030;
    if (xbox_va == 0x00246090u) return sub_00246090;
    if (xbox_va == 0x002460F0u) return sub_002460F0;
    if (xbox_va == 0x00246150u) return sub_00246150;
    if (xbox_va == 0x00246560u) return sub_00246560;
    if (xbox_va == 0x00246840u) return sub_00246840;
    if (xbox_va == 0x00246E1Bu) return sub_00246E1B;
    if (xbox_va == 0x002471B0u) return sub_002471B0;
    if (xbox_va == 0x0024737Au) return sub_0024737A;
    if (xbox_va == 0x0024BE00u) return sub_0024BE00;
    if (xbox_va == 0x00257DEAu) return sub_00257DEA;
    if (xbox_va == 0x00257E79u) return sub_00257E79;
    if (xbox_va == 0x002585ADu) return sub_002585AD;
    if (xbox_va == 0x00259E00u) return sub_00259E00;
    if (xbox_va == 0x00259EB0u) return sub_00259EB0;
    if (xbox_va == 0x00259EF8u) return sub_00259EF8;
    if (xbox_va == 0x0025A249u) return sub_0025A249;
    if (xbox_va == 0x0025A5D4u) return sub_0025A5D4;
    if (xbox_va == 0x0025A6E4u) return sub_0025A6E4;
    if (xbox_va == 0x0025AB29u) return sub_0025AB29;
    if (xbox_va == 0x0025DBEDu) return sub_0025DBED;
    if (xbox_va == 0x00262405u) return sub_00262405;
    if (xbox_va == 0x00262410u) return sub_00262410;
    if (xbox_va == 0x00262522u) return sub_00262522;
    if (xbox_va == 0x0026256Au) return sub_0026256A;
    if (xbox_va == 0x00266CC8u) return sub_00266CC8;
    if (xbox_va == 0x002681BFu) return sub_002681BF;
    if (xbox_va == 0x00268F41u) return sub_00268F41;
    if (xbox_va == 0x00268FEAu) return sub_00268FEA;
    if (xbox_va == 0x002698BDu) return sub_002698BD;
    if (xbox_va == 0x0026A17Eu) return sub_0026A17E;
    if (xbox_va == 0x0026B7D4u) return sub_0026B7D4;
    if (xbox_va == 0x0026E425u) return sub_0026E425;
    if (xbox_va == 0x0026FD01u) return sub_0026FD01;
    if (xbox_va == 0x00271AF6u) return sub_00271AF6;
    if (xbox_va == 0x00272DE4u) return sub_00272DE4;
    if (xbox_va == 0x00273FC6u) return sub_00273FC6;
    if (xbox_va == 0x00278278u) return sub_00278278;
    if (xbox_va == 0x00278ECDu) return sub_00278ECD;
    if (xbox_va == 0x002792C0u) return sub_002792C0;
    if (xbox_va == 0x00279D56u) return sub_00279D56;
    if (xbox_va == 0x00279E25u) return sub_00279E25;
    if (xbox_va == 0x00279E58u) return sub_00279E58;
    if (xbox_va == 0x00279F59u) return sub_00279F59;
    if (xbox_va == 0x0027A422u) return sub_0027A422;
    if (xbox_va == 0x0027B1D0u) return sub_0027B1D0;
    if (xbox_va == 0x0027B9B0u) return sub_0027B9B0;
    if (xbox_va == 0x0027C813u) return sub_0027C813;
    if (xbox_va == 0x0027CF1Cu) return sub_0027CF1C;
    if (xbox_va == 0x0027D4BAu) return sub_0027D4BA;
    if (xbox_va == 0x0027D724u) return sub_0027D724;
    if (xbox_va == 0x0027D982u) return sub_0027D982;
    if (xbox_va == 0x0027DA90u) return sub_0027DA90;
    if (xbox_va == 0x0027DC0Au) return sub_0027DC0A;
    if (xbox_va == 0x0027E327u) return sub_0027E327;
    if (xbox_va == 0x0027F326u) return sub_0027F326;
    if (xbox_va == 0x0027F3A7u) return sub_0027F3A7;
    if (xbox_va == 0x002804F0u) return sub_002804F0;
    if (xbox_va == 0x00282547u) return sub_00282547;
    if (xbox_va == 0x00285DB0u) return sub_00285DB0;
    if (xbox_va == 0x00287FA2u) return sub_00287FA2;
    if (xbox_va == 0x002895A0u) return sub_002895A0;
    if (xbox_va == 0x0029CE1Du) return sub_0029CE1D;
    if (xbox_va == 0x0029D1B4u) return sub_0029D1B4;
    if (xbox_va == 0x0029D368u) return sub_0029D368;
    if (xbox_va == 0x0029D671u) return sub_0029D671;
    if (xbox_va == 0x002A16BBu) return sub_002A16BB;
    if (xbox_va == 0x002A43A6u) return sub_002A43A6;
    if (xbox_va == 0x002A4FF7u) return sub_002A4FF7;
    if (xbox_va == 0x002A53B2u) return sub_002A53B2;
    if (xbox_va == 0x002A548Cu) return sub_002A548C;
    if (xbox_va == 0x002A5E17u) return sub_002A5E17;
    if (xbox_va == 0x002A70CCu) return sub_002A70CC;
    if (xbox_va == 0x002A7D56u) return sub_002A7D56;
    if (xbox_va == 0x002A9528u) return sub_002A9528;
    if (xbox_va == 0x002AA355u) return sub_002AA355;
    if (xbox_va == 0x002AC918u) return sub_002AC918;
    if (xbox_va == 0x002AFED8u) return sub_002AFED8;
    if (xbox_va == 0x002AFEEAu) return sub_002AFEEA;
    if (xbox_va == 0x002B094Fu) return sub_002B094F;
    if (xbox_va == 0x002B269Au) return sub_002B269A;
    if (xbox_va == 0x002B32FEu) return sub_002B32FE;
    if (xbox_va == 0x002B493Cu) return sub_002B493C;
    if (xbox_va == 0x002B51BFu) return sub_002B51BF;
    if (xbox_va == 0x002B52CDu) return sub_002B52CD;
    if (xbox_va == 0x002B8DFCu) return sub_002B8DFC;
    if (xbox_va == 0x002B9034u) return sub_002B9034;
    if (xbox_va == 0x002BB2FEu) return sub_002BB2FE;
    if (xbox_va == 0x002BB8DFu) return sub_002BB8DF;
    if (xbox_va == 0x002BC591u) return sub_002BC591;
    if (xbox_va == 0x002BEC39u) return sub_002BEC39;
    if (xbox_va == 0x002C0EB9u) return sub_002C0EB9;
    if (xbox_va == 0x002C0F75u) return sub_002C0F75;
    if (xbox_va == 0x002C15CCu) return sub_002C15CC;
    if (xbox_va == 0x002C1E88u) return sub_002C1E88;
    if (xbox_va == 0x002C207Eu) return sub_002C207E;
    if (xbox_va == 0x002C2A5Cu) return sub_002C2A5C;
    if (xbox_va == 0x002C2B7Eu) return sub_002C2B7E;
    if (xbox_va == 0x002C2B80u) return sub_002C2B80;
    if (xbox_va == 0x002C2DD6u) return sub_002C2DD6;
    if (xbox_va == 0x002C3073u) return sub_002C3073;
    if (xbox_va == 0x002C307Au) return sub_002C307A;
    if (xbox_va == 0x002C318Cu) return sub_002C318C;
    if (xbox_va == 0x002C3193u) return sub_002C3193;
    if (xbox_va == 0x002C3288u) return sub_002C3288;
    if (xbox_va == 0x002C36AFu) return sub_002C36AF;
    if (xbox_va == 0x002C3881u) return sub_002C3881;
    if (xbox_va == 0x002C3AB5u) return sub_002C3AB5;
    if (xbox_va == 0x002C3C51u) return sub_002C3C51;
    if (xbox_va == 0x002C7867u) return sub_002C7867;
    if (xbox_va == 0x002C78C5u) return sub_002C78C5;
    if (xbox_va == 0x002C78C8u) return sub_002C78C8;
    if (xbox_va == 0x002C7B40u) return sub_002C7B40;
    if (xbox_va == 0x002C7B85u) return sub_002C7B85;
    if (xbox_va == 0x002C85DAu) return sub_002C85DA;
    if (xbox_va == 0x002C87A3u) return sub_002C87A3;
    if (xbox_va == 0x002C87A8u) return sub_002C87A8;
    if (xbox_va == 0x002C87AFu) return sub_002C87AF;
    if (xbox_va == 0x002C9D75u) return sub_002C9D75;
    if (xbox_va == 0x002C9D94u) return sub_002C9D94;
    if (xbox_va == 0x002CA27Eu) return sub_002CA27E;
    if (xbox_va == 0x002CA7C7u) return sub_002CA7C7;
    if (xbox_va == 0x002CAB8Du) return sub_002CAB8D;
    if (xbox_va == 0x002CB2B6u) return sub_002CB2B6;
    if (xbox_va == 0x002CB2B8u) return sub_002CB2B8;
    if (xbox_va == 0x002CB305u) return sub_002CB305;
    if (xbox_va == 0x002CB348u) return sub_002CB348;
    if (xbox_va == 0x002CB368u) return sub_002CB368;
    if (xbox_va == 0x002CC832u) return sub_002CC832;
    if (xbox_va == 0x002CCC27u) return sub_002CCC27;
    if (xbox_va == 0x002CCE2Au) return sub_002CCE2A;
    if (xbox_va == 0x002CCE53u) return sub_002CCE53;
    if (xbox_va == 0x002CCEC0u) return sub_002CCEC0;
    if (xbox_va == 0x002CD068u) return sub_002CD068;
    if (xbox_va == 0x002CD091u) return sub_002CD091;
    if (xbox_va == 0x002CD5FDu) return sub_002CD5FD;
    if (xbox_va == 0x002CD6B2u) return sub_002CD6B2;
    if (xbox_va == 0x002CDEE8u) return sub_002CDEE8;
    if (xbox_va == 0x002CDEF6u) return sub_002CDEF6;
    if (xbox_va == 0x002CE193u) return sub_002CE193;
    if (xbox_va == 0x002CE1A7u) return sub_002CE1A7;
    if (xbox_va == 0x002CE7F3u) return sub_002CE7F3;
    if (xbox_va == 0x002CEA69u) return sub_002CEA69;
    if (xbox_va == 0x002CEBACu) return sub_002CEBAC;
    if (xbox_va == 0x002CF238u) return sub_002CF238;
    if (xbox_va == 0x002CF26Eu) return sub_002CF26E;
    if (xbox_va == 0x002CF2E4u) return sub_002CF2E4;
    if (xbox_va == 0x002CF5F3u) return sub_002CF5F3;
    if (xbox_va == 0x002D3140u) return sub_002D3140;
    if (xbox_va == 0x002D33ACu) return sub_002D33AC;
    if (xbox_va == 0x002D88D2u) return sub_002D88D2;
    if (xbox_va == 0x002DFBA0u) return sub_002DFBA0;
    if (xbox_va == 0x002DFC47u) return sub_002DFC47;
    if (xbox_va == 0x002E487Cu) return sub_002E487C;
    if (xbox_va == 0x002E4883u) return sub_002E4883;
    if (xbox_va == 0x002E495Eu) return sub_002E495E;
    if (xbox_va == 0x002E4F3Cu) return sub_002E4F3C;
    if (xbox_va == 0x002E4F43u) return sub_002E4F43;
    if (xbox_va == 0x002E501Eu) return sub_002E501E;
    if (xbox_va == 0x002E5DA4u) return sub_002E5DA4;
    if (xbox_va == 0x002E5DC5u) return sub_002E5DC5;
    if (xbox_va == 0x002E6506u) return sub_002E6506;
    if (xbox_va == 0x002E7073u) return sub_002E7073;
    if (xbox_va == 0x002E7200u) return sub_002E7200;
    if (xbox_va == 0x002E72FEu) return sub_002E72FE;
    if (xbox_va == 0x002E73B0u) return sub_002E73B0;
    if (xbox_va == 0x002E73DCu) return sub_002E73DC;
    if (xbox_va == 0x002E88EDu) return sub_002E88ED;
    if (xbox_va == 0x002EE4B1u) return sub_002EE4B1;
    if (xbox_va == 0x002EE6A1u) return sub_002EE6A1;
    if (xbox_va == 0x002EE6A2u) return sub_002EE6A2;
    if (xbox_va == 0x002EE843u) return sub_002EE843;
    if (xbox_va == 0x002EE844u) return sub_002EE844;
    if (xbox_va == 0x002EE892u) return sub_002EE892;
    if (xbox_va == 0x002EF134u) return sub_002EF134;
    if (xbox_va == 0x002EF4C3u) return sub_002EF4C3;
    if (xbox_va == 0x002EF4F3u) return sub_002EF4F3;
    if (xbox_va == 0x002EF938u) return sub_002EF938;
    if (xbox_va == 0x002EFCFEu) return sub_002EFCFE;
    if (xbox_va == 0x002EFFE3u) return sub_002EFFE3;
    if (xbox_va == 0x002F032Au) return sub_002F032A;
    if (xbox_va == 0x002F0334u) return sub_002F0334;
    if (xbox_va == 0x002F0344u) return sub_002F0344;
    if (xbox_va == 0x002F04EDu) return sub_002F04ED;
    if (xbox_va == 0x002F1336u) return sub_002F1336;
    if (xbox_va == 0x002F1350u) return sub_002F1350;
    if (xbox_va == 0x002F1352u) return sub_002F1352;
    if (xbox_va == 0x002F13A6u) return sub_002F13A6;
    if (xbox_va == 0x002F14EDu) return sub_002F14ED;
    if (xbox_va == 0x002F1A7Bu) return sub_002F1A7B;
    if (xbox_va == 0x002F269Au) return sub_002F269A;
    if (xbox_va == 0x002F28DDu) return sub_002F28DD;
    if (xbox_va == 0x002F293Du) return sub_002F293D;
    if (xbox_va == 0x002F293Fu) return sub_002F293F;
    if (xbox_va == 0x002F29C5u) return sub_002F29C5;
    if (xbox_va == 0x002F29CBu) return sub_002F29CB;
    if (xbox_va == 0x002F29ECu) return sub_002F29EC;
    if (xbox_va == 0x002F35ADu) return sub_002F35AD;
    if (xbox_va == 0x002F55ECu) return sub_002F55EC;
    if (xbox_va == 0x002F56F8u) return sub_002F56F8;
    if (xbox_va == 0x002F626Du) return sub_002F626D;
    if (xbox_va == 0x002F665Cu) return sub_002F665C;
    if (xbox_va == 0x002F6687u) return sub_002F6687;
    if (xbox_va == 0x002F66A7u) return sub_002F66A7;
    if (xbox_va == 0x002F6CEFu) return sub_002F6CEF;
    if (xbox_va == 0x002F703Cu) return sub_002F703C;
    if (xbox_va == 0x002F70D7u) return sub_002F70D7;
    if (xbox_va == 0x002F7129u) return sub_002F7129;
    if (xbox_va == 0x002F715Fu) return sub_002F715F;
    if (xbox_va == 0x002F744Au) return sub_002F744A;
    if (xbox_va == 0x002F744Bu) return sub_002F744B;
    if (xbox_va == 0x002F7481u) return sub_002F7481;
    if (xbox_va == 0x002F78FDu) return sub_002F78FD;
    if (xbox_va == 0x002F97B4u) return sub_002F97B4;
    if (xbox_va == 0x002F97B7u) return sub_002F97B7;
    if (xbox_va == 0x002FB3D9u) return sub_002FB3D9;
    if (xbox_va == 0x002FB6E5u) return sub_002FB6E5;
    if (xbox_va == 0x002FB71Eu) return sub_002FB71E;
    if (xbox_va == 0x002FB9E6u) return sub_002FB9E6;
    if (xbox_va == 0x002FBA95u) return sub_002FBA95;
    if (xbox_va == 0x002FBBEEu) return sub_002FBBEE;
    if (xbox_va == 0x002FBC21u) return sub_002FBC21;
    if (xbox_va == 0x002FBE57u) return sub_002FBE57;
    if (xbox_va == 0x002FC3D6u) return sub_002FC3D6;
    if (xbox_va == 0x002FD866u) return sub_002FD866;
    if (xbox_va == 0x002FDDA3u) return sub_002FDDA3;
    if (xbox_va == 0x002FE0FDu) return sub_002FE0FD;
    if (xbox_va == 0x002FE2ADu) return sub_002FE2AD;
    if (xbox_va == 0x002FEBD2u) return sub_002FEBD2;
    if (xbox_va == 0x002FEC48u) return sub_002FEC48;
    if (xbox_va == 0x002FF055u) return sub_002FF055;
    if (xbox_va == 0x002FF058u) return sub_002FF058;
    if (xbox_va == 0x002FF2C8u) return sub_002FF2C8;
    if (xbox_va == 0x002FF373u) return sub_002FF373;
    if (xbox_va == 0x002FF376u) return sub_002FF376;
    if (xbox_va == 0x002FF5A8u) return sub_002FF5A8;
    if (xbox_va == 0x002FF814u) return sub_002FF814;
    if (xbox_va == 0x002FFB60u) return sub_002FFB60;
    if (xbox_va == 0x002FFD9Du) return sub_002FFD9D;
    if (xbox_va == 0x002FFFBAu) return sub_002FFFBA;
    if (xbox_va == 0x0030015Eu) return sub_0030015E;
    if (xbox_va == 0x00300920u) return sub_00300920;
    if (xbox_va == 0x00300932u) return sub_00300932;
    if (xbox_va == 0x00304010u) return sub_00304010;
    if (xbox_va == 0x0030473Bu) return sub_0030473B;
    if (xbox_va == 0x00307010u) return sub_00307010;
    if (xbox_va == 0x0030765Fu) return sub_0030765F;
    if (xbox_va == 0x00307A9Du) return sub_00307A9D;
    if (xbox_va == 0x0030801Au) return sub_0030801A;
    if (xbox_va == 0x00308489u) return sub_00308489;
    if (xbox_va == 0x00308820u) return sub_00308820;
    if (xbox_va == 0x003093CDu) return sub_003093CD;
    if (xbox_va == 0x00313748u) return sub_00313748;
    if (xbox_va == 0x00314383u) return sub_00314383;
    if (xbox_va == 0x00314C10u) return sub_00314C10;
    if (xbox_va == 0x00317EBDu) return sub_00317EBD;
    if (xbox_va == 0x003180B2u) return sub_003180B2;
    if (xbox_va == 0x003189F4u) return sub_003189F4;
    if (xbox_va == 0x00319859u) return sub_00319859;
    if (xbox_va == 0x0031A2EDu) return sub_0031A2ED;
    if (xbox_va == 0x0031AF26u) return sub_0031AF26;
    if (xbox_va == 0x0031D389u) return sub_0031D389;
    if (xbox_va == 0x0031D9ACu) return sub_0031D9AC;
    if (xbox_va == 0x0031EBAAu) return sub_0031EBAA;
    if (xbox_va == 0x0031F72Au) return sub_0031F72A;
    if (xbox_va == 0x00322220u) return sub_00322220;
    if (xbox_va == 0x003222CBu) return sub_003222CB;
    if (xbox_va == 0x00322530u) return sub_00322530;
    if (xbox_va == 0x00323AFDu) return sub_00323AFD;
    if (xbox_va == 0x003244BCu) return sub_003244BC;
    if (xbox_va == 0x00325A05u) return sub_00325A05;
    if (xbox_va == 0x00325AAAu) return sub_00325AAA;
    if (xbox_va == 0x00325C0Fu) return sub_00325C0F;
    if (xbox_va == 0x00325C9Eu) return sub_00325C9E;
    if (xbox_va == 0x00326448u) return sub_00326448;
    if (xbox_va == 0x00326650u) return sub_00326650;
    if (xbox_va == 0x003266F7u) return sub_003266F7;
    if (xbox_va == 0x00328018u) return sub_00328018;
    if (xbox_va == 0x003287F5u) return sub_003287F5;
    if (xbox_va == 0x00328864u) return sub_00328864;
    if (xbox_va == 0x00328B40u) return sub_00328B40;
    if (xbox_va == 0x00328BACu) return sub_00328BAC;
    if (xbox_va == 0x0032A2E2u) return sub_0032A2E2;
    if (xbox_va == 0x0032A8D3u) return sub_0032A8D3;
    if (xbox_va == 0x0032A900u) return sub_0032A900;
    if (xbox_va == 0x0032C633u) return sub_0032C633;
    if (xbox_va == 0x0032C661u) return sub_0032C661;
    if (xbox_va == 0x0032C690u) return sub_0032C690;
    if (xbox_va == 0x0032C7E0u) return sub_0032C7E0;
    if (xbox_va == 0x0032C816u) return sub_0032C816;
    if (xbox_va == 0x0033A822u) return sub_0033A822;
    if (xbox_va == 0x0033D4EFu) return sub_0033D4EF;
    if (xbox_va == 0x0033D75Du) return sub_0033D75D;
    if (xbox_va == 0x0033F381u) return sub_0033F381;
    if (xbox_va == 0x0033F6B0u) return sub_0033F6B0;
    if (xbox_va == 0x00340CDEu) return sub_00340CDE;
    if (xbox_va == 0x00340D86u) return sub_00340D86;
    if (xbox_va == 0x00341137u) return sub_00341137;
    if (xbox_va == 0x003412E1u) return sub_003412E1;
    if (xbox_va == 0x003412EAu) return sub_003412EA;
    if (xbox_va == 0x00341438u) return sub_00341438;
    if (xbox_va == 0x00341699u) return sub_00341699;
    if (xbox_va == 0x00341740u) return sub_00341740;
    if (xbox_va == 0x00341935u) return sub_00341935;
    if (xbox_va == 0x00341936u) return sub_00341936;
    if (xbox_va == 0x00341A2Cu) return sub_00341A2C;
    if (xbox_va == 0x003421E8u) return sub_003421E8;
    if (xbox_va == 0x00342384u) return sub_00342384;
    if (xbox_va == 0x003424E5u) return sub_003424E5;
    if (xbox_va == 0x003426D6u) return sub_003426D6;
    if (xbox_va == 0x00342708u) return sub_00342708;
    if (xbox_va == 0x0034299Du) return sub_0034299D;
    if (xbox_va == 0x00343569u) return sub_00343569;
    if (xbox_va == 0x0034360Du) return sub_0034360D;
    if (xbox_va == 0x00343862u) return sub_00343862;
    if (xbox_va == 0x00343AD0u) return sub_00343AD0;
    if (xbox_va == 0x00343D1Cu) return sub_00343D1C;
    if (xbox_va == 0x00343E62u) return sub_00343E62;
    if (xbox_va == 0x00343E65u) return sub_00343E65;
    if (xbox_va == 0x00343FE1u) return sub_00343FE1;
    if (xbox_va == 0x00344878u) return sub_00344878;
    if (xbox_va == 0x0034558Au) return sub_0034558A;
    if (xbox_va == 0x00345DC0u) return sub_00345DC0;
    if (xbox_va == 0x00345E24u) return sub_00345E24;
    if (xbox_va == 0x00346743u) return sub_00346743;
    if (xbox_va == 0x00347E1Fu) return sub_00347E1F;
    if (xbox_va == 0x00347E6Au) return sub_00347E6A;
    if (xbox_va == 0x00348673u) return sub_00348673;
    if (xbox_va == 0x003486B0u) return sub_003486B0;
    if (xbox_va == 0x003489E3u) return sub_003489E3;
    if (xbox_va == 0x00348A7Eu) return sub_00348A7E;
    if (xbox_va == 0x00348AE1u) return sub_00348AE1;
    if (xbox_va == 0x00348AF2u) return sub_00348AF2;
    if (xbox_va == 0x00348C8Au) return sub_00348C8A;
    if (xbox_va == 0x00348CECu) return sub_00348CEC;
    if (xbox_va == 0x00349183u) return sub_00349183;
    if (xbox_va == 0x0034921Bu) return sub_0034921B;
    if (xbox_va == 0x003494FFu) return sub_003494FF;
    if (xbox_va == 0x00349F24u) return sub_00349F24;
    if (xbox_va == 0x00349FABu) return sub_00349FAB;
    if (xbox_va == 0x0034A3F1u) return sub_0034A3F1;
    if (xbox_va == 0x0034A57Du) return sub_0034A57D;
    if (xbox_va == 0x0034A627u) return sub_0034A627;
    if (xbox_va == 0x0034A66Fu) return sub_0034A66F;
    if (xbox_va == 0x0034A744u) return sub_0034A744;
    if (xbox_va == 0x0034A9C3u) return sub_0034A9C3;
    if (xbox_va == 0x0034AA86u) return sub_0034AA86;
    if (xbox_va == 0x0034AF6Cu) return sub_0034AF6C;
    if (xbox_va == 0x0034B6B7u) return sub_0034B6B7;
    if (xbox_va == 0x0034BB3Au) return sub_0034BB3A;
    if (xbox_va == 0x0034BF1Bu) return sub_0034BF1B;
    if (xbox_va == 0x0034C32Bu) return sub_0034C32B;
    if (xbox_va == 0x0034C59Du) return sub_0034C59D;
    if (xbox_va == 0x0034C6C0u) return sub_0034C6C0;
    if (xbox_va == 0x0034C79Eu) return sub_0034C79E;
    if (xbox_va == 0x0034C8A7u) return sub_0034C8A7;
    if (xbox_va == 0x0034CC9Cu) return sub_0034CC9C;
    if (xbox_va == 0x0034CC9Fu) return sub_0034CC9F;
    if (xbox_va == 0x0034CCB5u) return sub_0034CCB5;
    if (xbox_va == 0x0034CD6Fu) return sub_0034CD6F;
    if (xbox_va == 0x0034D06Au) return sub_0034D06A;
    if (xbox_va == 0x0034D189u) return sub_0034D189;
    if (xbox_va == 0x0034D453u) return sub_0034D453;
    if (xbox_va == 0x0034D9A8u) return sub_0034D9A8;
    if (xbox_va == 0x0034DD08u) return sub_0034DD08;
    if (xbox_va == 0x003556E0u) return sub_003556E0;
    if (xbox_va == 0x0035ADA0u) return sub_0035ADA0;
    if (xbox_va == 0x0035ADB0u) return sub_0035ADB0;
    if (xbox_va == 0x00361720u) return sub_00361720;
    if (xbox_va == 0x00361EF0u) return sub_00361EF0;
    if (xbox_va == 0x00368D90u) return sub_00368D90;
    if (xbox_va == 0x00368E40u) return sub_00368E40;
    if (xbox_va == 0x00368EA0u) return sub_00368EA0;
    if (xbox_va == 0x0036F330u) return sub_0036F330;
    if (xbox_va == 0x0036F348u) return sub_0036F348;
    if (xbox_va == 0x0036F350u) return sub_0036F350;
    if (xbox_va == 0x0036F352u) return sub_0036F352;
    if (xbox_va == 0x0036F535u) return sub_0036F535;
    if (xbox_va == 0x0036F5F2u) return sub_0036F5F2;
    if (xbox_va == 0x0036F6DEu) return sub_0036F6DE;
    if (xbox_va == 0x0036F70Fu) return sub_0036F70F;
    if (xbox_va == 0x0036F73Du) return sub_0036F73D;
    if (xbox_va == 0x0036F7A8u) return sub_0036F7A8;
    if (xbox_va == 0x0036F7C9u) return sub_0036F7C9;
    if (xbox_va == 0x0036F7D2u) return sub_0036F7D2;
    if (xbox_va == 0x0036F7DAu) return sub_0036F7DA;
    if (xbox_va == 0x00370666u) return sub_00370666;
    if (xbox_va == 0x0037272Bu) return sub_0037272B;
    if (xbox_va == 0x00396040u) return sub_00396040;
    if (xbox_va == 0x00396060u) return sub_00396060;
    if (xbox_va == 0x003971C7u) return sub_003971C7;
    if (xbox_va == 0x00397247u) return sub_00397247;
    if (xbox_va == 0x0039724Bu) return sub_0039724B;
    if (xbox_va == 0x0039737Du) return sub_0039737D;
    if (xbox_va == 0x0039737Eu) return sub_0039737E;
    if (xbox_va == 0x0039737Fu) return sub_0039737F;
    if (xbox_va == 0x00397386u) return sub_00397386;
    if (xbox_va == 0x00397400u) return sub_00397400;
    if (xbox_va == 0x00397480u) return sub_00397480;
    if (xbox_va == 0x00397481u) return sub_00397481;
    if (xbox_va == 0x00397556u) return sub_00397556;
    if (xbox_va == 0x00397598u) return sub_00397598;
    if (xbox_va == 0x00397618u) return sub_00397618;
    if (xbox_va == 0x00397732u) return sub_00397732;

    /* Video playback shim overrides - Phase 1: stub to return success immediately */
    if (xbox_va == 0x00340FEB) return sub_00340FEB;
    if (xbox_va == 0x003432A8) return sub_003432A8;
    if (xbox_va == 0x003464F1) return sub_003464F1;
    if (xbox_va == 0x003467B6) return sub_003467B6;
    if (xbox_va == 0x003467F2) return sub_003467F2;

    /* Network fallback override - stub to prevent blocking in main thread */
    if (xbox_va == 0x00345AB0) return sub_00345AB0;

    if (xbox_va == 0x001A237Du) return sub_001A237D_stub;  /* XapiBootToDash */
    if (xbox_va == 0x0019F196) return sub_0019F196;
    if (xbox_va == 0x001A1C23) return sub_001A1C23;
    return (recomp_func_t)0;
}

/*
 * sub_001A016A: direct-call replacement (not a recomp_lookup_manual
 * override - sub_00345ACC calls it with a plain C call). Real logic
 * disabled in recomp_0011.c (#if 0). See DEBUGGING_NOTES.md.
 *
 * Queries a flag byte at [ptr-0xB] on what looks like a per-thread C++
 * exception-state object. Crashes when that pointer is NULL - i.e. when
 * no C++ exception has ever been thrown on this thread, so no state was
 * ever allocated. Real CRT startup guarantees this state exists before
 * any code can reach this query; we skip full CRT startup, so the
 * guarantee doesn't hold. Treat "no state" the same as "flag bit 0 is
 * clear" (the existing failure path already handles that correctly via
 * the real generated sub_001A017A/sub_001A0196), rather than crashing.
 */
extern void sub_001A017A(void);
extern void sub_001A0196(void);

void sub_001A016A(void)
{
    g_ecx = MEM32(g_esp + 0xC);
    if (g_ecx == 0) {
        g_eax = 0xFFFFFFFFu;
        g_esp += 16;  /* matches sub_001A0196's own cleanup (ret 12 + retaddr) */
        return;
    }
    g_eax = MEM8(g_ecx - 11);
    if (g_eax & 1) { sub_001A017A(); return; }
    g_eax = 0xFFFFFFFFu;
    sub_001A0196();
}

/*
 * sub_0019F765: direct-call replacement (not a recomp_lookup_manual
 * override - sub_001A02B7 calls it with a plain C call). Real logic
 * disabled in recomp_0011.c (#if 0). See DEBUGGING_NOTES.md ("heap
 * manager's large-block allocation path").
 *
 * Walks a linked list of range entries starting at [bucket_ptr+0x38],
 * used by the heap allocator's size-class bucket search. On real
 * hardware the list is either NULL-terminated or fully populated; in
 * our environment a not-yet-found gap in heap segment/UCR bookkeeping
 * means the list can start with a non-null but invalid pointer (garbage,
 * not a real Xbox VA) instead. This adds a plausible-Xbox-VA bounds
 * check before each dereference, treating an invalid pointer the same
 * as a clean NULL (end of list) instead of crashing. This masks the
 * underlying gap rather than fixing it - the allocation this protects
 * may return an unexpected block or fail where it should succeed.
 * Revisit if that turns out to matter once further along.
 */
extern void sub_0019F7AB(void);

void sub_0019F765(void)
{
    uint32_t entry_esp = g_esp;
    uint32_t bucket_ptr = MEM32(entry_esp + 8);
    uint32_t key1_ptr = MEM32(entry_esp + 0xC);
    uint32_t key2 = MEM32(entry_esp + 0x10);
    uint32_t key1 = MEM32(key1_ptr);
    uint32_t list_ptr = MEM32(bucket_ptr + 0x38);

    while (list_ptr != 0) {
        if (list_ptr < 0x00010000u || list_ptr >= 0x74000000u) {
            break;  /* garbage, not a real Xbox VA - treat as end of list */
        }
        if (MEM32(list_ptr + 8) >= key1) {
            if (key2 == 0 || MEM32(list_ptr + 4) == key2) {
                /* Found - tail into sub_0019F7AB sharing this frame, matching
                 * the original's plain jmp (it reads g_esi and [ebp+8], and
                 * reuses the [ebp+0xC] slot as scratch - don't touch g_esp). */
                g_esi = list_ptr;
                g_seh_ebp = entry_esp - 4;
                sub_0019F7AB();
                return;
            }
        }
        list_ptr = MEM32(list_ptr);
    }

    g_eax = 0;
    g_esp = entry_esp + 20;  /* ret 0x10 */
}

/*
 * sub_001F8890 - string/name pool allocator. Called thiscall-style with
 * the pool object in ecx and (length, out-ptr, out-ptr) on the stack;
 * returns a buffer in eax that the caller immediately memcpy's a string
 * into (no null check at the call site - see recomp_0015.c).
 *
 * On real hardware ecx is never null. In our build it IS null, because
 * whatever constructs this pool is D3D-related (D3D isn't recompiled -
 * see DEBUGGING_NOTES.md) and the generated version spins forever walking
 * a linked list through a null pointer. Rather than emulate the real
 * pool's free-list, just hand back a fresh block from the bump allocator
 * - the caller only wants a writable buffer of the right size.
 */
void sub_001F8890(void)
{
    uint32_t entry_esp = g_esp;
    uint32_t len = MEM32(entry_esp + 4);

    g_eax = xbox_HeapAlloc(len ? len : 4, 4);
    g_esp = entry_esp + 16;  /* ret 12 */
}

extern void sub_001F853F(void);
extern void sub_001F8545(void);

/*
 * sub_001F84D0 - font/glyph table lookup. Identical to the generated
 * version (see the #if 0 block in recomp_0014.c) except for one added
 * guard: the table pointer it reads from a D3D-owned global (0x5BC538 /
 * 0x5BC53C, selected by a flag byte) is never initialized since D3D isn't
 * recompiled (see DEBUGGING_NOTES.md), and walking a null table through
 * the vtable dispatch below eventually reads out of bounds and crashes.
 * On real hardware that pointer is never null; take the same "nothing to
 * render" early-out the function already uses for esi==0 / ebx==0.
 *
 * Kept as a near-verbatim copy (not reimplemented from scratch) because
 * the stack bookkeeping here is entangled with sub_001F853F/sub_001F8545
 * (shared-epilogue-style tail calls, no explicit esp cleanup in this
 * function) - safer to preserve exactly and only add the one guard.
 */
void sub_001F84D0(void)
{
    uint32_t ebp;
    ebp = g_seh_ebp;

    PUSH32(g_esp, g_ecx);
    PUSH32(g_esp, g_ebx);
    PUSH32(g_esp, ebp);
    PUSH32(g_esp, g_esi);
    g_esi = MEM32(g_esp + 0x14);
    ebp = g_ecx;
    g_ecx = MEM32(ebp + 8);
    g_eax = 0;
    MEM32(g_esp + 0xC) = g_ecx;
    if (CMP_EQ(g_esi, g_eax)) { g_seh_ebp = ebp; sub_001F853F(); return; }

    g_ebx = MEM32(g_esp + 0x18);
    if (CMP_EQ(g_ebx, g_eax)) { g_seh_ebp = ebp; sub_001F853F(); return; }

    g_eax = ZX8(MEM8(ebp + 7));
    g_ecx = MEM32(0x5BC53C);
    if (!TEST_NZ(LO8(g_eax), 1)) g_ecx = MEM32(0x5BC538);

    if (g_ecx == 0) { g_seh_ebp = ebp; sub_001F853F(); return; }

    g_edx = MEM32(g_ecx);
    g_eax = (uint32_t)((int32_t)g_eax >> 1);
    g_eax = MEM32(g_edx + g_eax * 4);
    g_edx = MEM32(g_eax);
    { uint32_t _icall_esp = g_esp;
    PUSH32(g_esp, g_edi);
    g_ecx = g_ebx + 1;
    PUSH32(g_esp, g_ecx);
    g_ecx = g_eax;
    PUSH32(g_esp, 0);
#define eax g_eax /* RECOMP_ICALL_SAFE assumes the generated-code register aliases */
    RECOMP_ICALL_SAFE(MEM32(g_edx + 0xCC), _icall_esp);
#undef eax
    }

    g_ecx = g_ebx;
    g_edx = g_ecx;
    g_ecx = g_ecx >> 2;
    MEM32(ebp + 8) = g_eax;
    g_edi = g_eax;
    memcpy((void*)XBOX_PTR(g_edi), (void*)XBOX_PTR(g_esi), g_ecx * 4);
    g_esi += g_ecx * 4; g_edi += g_ecx * 4; g_ecx = 0;
    g_ecx = g_edx;
    g_ecx = g_ecx & 3;
    memcpy((void*)XBOX_PTR(g_edi), (void*)XBOX_PTR(g_esi), g_ecx);
    g_esi += g_ecx; g_edi += g_ecx; g_ecx = 0;
    g_eax = MEM32(ebp + 8);
    g_ecx = MEM32(g_esp + 0x10);
    MEM8(g_ebx + g_eax) = 0;
    MEM32(ebp + 0xC) = g_ebx;
    POP32(g_esp, g_edi);
    g_seh_ebp = ebp; sub_001F8545(); return;
}

extern void sub_00202B60(void);

/*
 * sub_002085CA - append-to-dynamic-array. Same D3D-null pattern again:
 * growth (sub_00202B60) routes through the D3D-dependent allocator
 * selected via 0x5BC53C/0x5BC538 (see sub_001F84D0 above), which always
 * fails, so the array's backing buffer (esi+8) never gets allocated. The
 * generated version writes into it unconditionally and crashes.
 *
 * Only one caller (sub_002085A0's tail-call, recomp_0015.c) with a fixed,
 * simple register/stack contract, so kept as a near-verbatim copy (same
 * reasoning as sub_001F84D0) plus one added guard: skip the write (and
 * the count increment) if the buffer is still null, rather than crashing.
 */
void sub_002085CA(void)
{
    if (!CMP_L(g_ebx, g_eax)) {
        PUSH32(g_esp, g_ebx);
        g_ecx = g_esi;
        PUSH32(g_esp, 0);
        sub_00202B60();
    }

    g_eax = MEM32(g_esi + 0xC);
    g_ecx = MEM32(g_esi + 8);
    g_edx = MEM32(g_esp + 0x10);
    if (g_ecx != 0) {
        MEM32(g_ecx + g_eax * 4) = g_edx;
        g_eax = MEM32(g_esi + 0xC);
        g_ecx = g_eax + 1;
        MEM32(g_esi + 0xC) = g_ecx;
    }
    POP32(g_esp, g_edi);
    POP32(g_esp, g_esi);
    POP32(g_esp, g_ebx);
    g_esp += 8;
}

/* ── ICALL failure logging ─────────────────────────────────── */

/*
 * Called when RECOMP_ICALL cannot resolve a target address.
 * This usually means one of:
 *   - A vtable dispatch to an address not in the dispatch table
 *   - A function pointer loaded from uninitialized or corrupt memory
 *   - A kernel thunk address that the bridge doesn't handle
 *
 * During early bring-up you will see many of these. Most are harmless
 * (the ICALL macro pops the dummy return address and continues).
 * Focus on the ones that cause crashes or incorrect behavior.
 *
 * Deduped by address (log each failing VA once) - this fires on every
 * failed indirect call at runtime, which for a hot vtable dispatch could
 * otherwise be thousands of lines for the same address. Bounded to the
 * first 512 unique addresses; further ones are silently dropped (a note
 * is printed once) rather than growing unbounded.
 */
void recomp_icall_fail_log(uint32_t va)
{
    static uint32_t seen[512];
    static int seen_count = 0;
    static int overflowed = 0;

    /* One-shot trace that bypasses the dedup below: fires on the Nth total
     * failure regardless of VA, to catch a spin loop repeatedly failing on
     * an already-seen VA (which the dedup would otherwise silence). */
    static uint64_t total_fails = 0;
    total_fails++;
    if (total_fails == 1000) {
        fprintf(stderr, "[ICALL] Spin-loop probe: failure #1000, VA 0x%08X (total calls: %llu)\n",
                va, (unsigned long long)g_icall_count);
        void *frames[8];
        USHORT n = CaptureStackBackTrace(1, 8, frames, NULL);
        HMODULE mod = GetModuleHandleA(NULL);
        fprintf(stderr, "  Native call stack (resolve against build/*.map):\n");
        for (USHORT i = 0; i < n; i++) {
            fprintf(stderr, "    [%u] RVA 0x%llX\n", i,
                (unsigned long long)((BYTE *)frames[i] - (BYTE *)mod));
        }
        fflush(stderr);
    }

    for (int i = 0; i < seen_count; i++) {
        if (seen[i] == va) return;
    }
    if (seen_count < 512) {
        seen[seen_count++] = va;
    } else if (!overflowed) {
        overflowed = 1;
        fprintf(stderr, "[ICALL] 512 unique failing addresses logged - suppressing further new ones\n");
    } else {
        return;
    }

    fprintf(stderr, "[ICALL] Failed to resolve VA 0x%08X (total calls: %llu)\n",
            va, (unsigned long long)g_icall_count);

    /* Dump last 16 call targets from the ring buffer */
    fprintf(stderr, "  Recent ICALL targets:\n");
    for (int i = 0; i < 16; i++) {
        int idx = (g_icall_trace_idx - 16 + i) & 15;
        if (g_icall_trace[idx])
            fprintf(stderr, "    [%2d] 0x%08X\n", i, g_icall_trace[idx]);
    }

    /* Native call stack of the C caller - resolve as RVAs (addr - module
     * base) against build/*.map "Publics by Value" to find the generated
     * sub_XXXXXXXX function that issued this ICALL. */
    void *frames[8];
    USHORT n = CaptureStackBackTrace(1, 8, frames, NULL);
    HMODULE mod = GetModuleHandleA(NULL);
    fprintf(stderr, "  Native call stack (resolve against build/*.map):\n");
    for (USHORT i = 0; i < n; i++) {
        fprintf(stderr, "    [%u] RVA 0x%llX\n", i,
            (unsigned long long)((BYTE *)frames[i] - (BYTE *)mod));
    }
    fflush(stderr);
}

/*
 * Network fallback stub - prevents blocking in main thread.
 * The original sub_00345AB0 calls into network code that blocks waiting
 * for a connection. On PC without Xbox Live, this blocks forever.
 * This stub returns S_OK (0) immediately to unblock the main thread.
 */
void sub_00345AB0(void)
{
    g_eax = 0;  /* S_OK */
    /* Caller (cdecl) cleans the argument from the stack */
}

/* ── ICALL trace ring buffer ───────────────────────────────── */

/*
 * These globals are written by the RECOMP_ICALL macro (defined in
 * recomp_types.h) every time an indirect call is dispatched. When a
 * crash occurs, the VEH handler or recomp_icall_fail_log() can dump
 * the last 16 call targets to help you trace what happened.
 *
 * Already defined in xbox_kernel (src/kernel/xbox_memory_layout.c) -
 * declare extern here instead of redefining.
 */
extern volatile uint32_t g_icall_trace[16];
extern volatile uint32_t g_icall_trace_idx;
extern volatile uint64_t g_icall_count;

/* ── ICALL safe recovery ─────────────────────────────────────── */

/*
 * When an ICALL target is invalid (corrupted function pointer), the
 * RECOMP_ICALL_SAFE macro calls sub_00ICALL_SAFE_STUB instead of
 * crashing. To prevent the caller from using the bogus return value
 * (0) as a function pointer, we loop forever instead of returning,
 * allowing a debugger to be attached for investigation.
 */
/* ── Safe no-op function for invalid ICALL targets ───────────── */

/*
 * A harmless no-op function that can be safely called when an ICALL
 * target is invalid. It does nothing and returns 0 (S_OK).
 */
void sub_00ICALL_SAFE_NOOP(void)
{
    /* Do nothing, return 0 (S_OK) */
}

/*
 * Safe ICALL stub - called when an indirect call target is invalid.
 * Instead of crashing by calling 0, this sets g_eax to a safe no-op
 * function address so the caller can safely "call eax" without crashing.
 */
/* ── Rejected-target histogram ───────────────────────────────── */

/*
 * The plausibility filter's [0x00400000, 0xFE000000) branch used to reject
 * silently, so a spin could burn 80k calls without ever naming the target.
 * Distinct targets are few in practice, so a linear table is plenty.
 *
 * ponytail: fixed 32 slots, linear scan. If a run ever overflows it the
 * summary says so - swap for a hash map only if that actually happens.
 */
#define REJECT_SLOTS 32
static struct { uint32_t va; uint64_t count; } g_reject[REJECT_SLOTS];
static unsigned g_reject_used;
static uint64_t g_reject_total, g_reject_dropped;

void recomp_icall_reject_dump(void)
{
    if (!g_reject_total)
        return;
    fprintf(stderr, "\n[ICALL-REJECT] %llu call(s) rejected by the plausibility "
                    "filter, %u distinct target(s):\n",
            (unsigned long long)g_reject_total, g_reject_used);
    for (unsigned i = 0; i < g_reject_used; i++)
        fprintf(stderr, "    0x%08X  x%llu\n",
                g_reject[i].va, (unsigned long long)g_reject[i].count);
    if (g_reject_dropped)
        fprintf(stderr, "    (%llu more from >%d distinct targets - table full)\n",
                (unsigned long long)g_reject_dropped, REJECT_SLOTS);
    fflush(stderr);
}

void recomp_icall_reject_log(uint32_t va)
{
    static int registered;
    if (!registered) {          /* dump the summary however we exit */
        registered = 1;
        atexit(recomp_icall_reject_dump);
    }
    g_reject_total++;
    for (unsigned i = 0; i < g_reject_used; i++) {
        if (g_reject[i].va == va) {
            g_reject[i].count++;
            return;
        }
    }
    if (g_reject_used < REJECT_SLOTS) {
        g_reject[g_reject_used].va = va;
        g_reject[g_reject_used].count = 1;
        g_reject_used++;
        /* First sighting is the useful one - print it with a backtrace hook. */
        fprintf(stderr, "[ICALL-REJECT] new target 0x%08X (rejected #%llu)\n",
                va, (unsigned long long)g_reject_total);
        fflush(stderr);
    } else {
        g_reject_dropped++;
    }
}

/* ── Unresolved-stub hit tracking ────────────────────────────── */

/*
 * recomp_stubs_unresolved.c holds ~3,200 empty bodies for mid-function
 * addresses the disassembler never classified as functions. An empty body
 * returns without running the rest of the real function - so any prologue
 * pushes never get popped, and callee-saved registers are never restored.
 *
 * Knowing *which* stubs actually execute is the whole game: it turns "3,200
 * possible culprits" into a short list. tools_data/trace_stubs.py rewrites the
 * stub bodies to call this; --off restores them.
 */
#define STUB_SLOTS 64
static struct { uint32_t va; uint64_t count; } g_stub[STUB_SLOTS];
static unsigned g_stub_used;
static uint64_t g_stub_total, g_stub_dropped;

void recomp_stub_dump(void)
{
    if (!g_stub_total)
        return;
    fprintf(stderr, "\n[STUB-HIT] %llu call(s) into unresolved stubs, "
                    "%u distinct:\n",
            (unsigned long long)g_stub_total, g_stub_used);
    for (unsigned i = 0; i < g_stub_used; i++)
        fprintf(stderr, "    0x%08X  x%llu\n",
                g_stub[i].va, (unsigned long long)g_stub[i].count);
    if (g_stub_dropped)
        fprintf(stderr, "    (%llu more beyond %d distinct - table full)\n",
                (unsigned long long)g_stub_dropped, STUB_SLOTS);
    fflush(stderr);
}

void recomp_stub_hit(uint32_t va)
{
    static int registered;
    if (!registered) {
        registered = 1;
        atexit(recomp_stub_dump);
    }
    g_stub_total++;
    for (unsigned i = 0; i < g_stub_used; i++) {
        if (g_stub[i].va == va) {
            g_stub[i].count++;
            return;
        }
    }
    if (g_stub_used < STUB_SLOTS) {
        g_stub[g_stub_used].va = va;
        g_stub[g_stub_used].count = 1;
        g_stub_used++;
        fprintf(stderr, "[STUB-HIT] first entry into 0x%08X (hit #%llu)\n",
                va, (unsigned long long)g_stub_total);
        fflush(stderr);
    } else {
        g_stub_dropped++;
    }
}

void sub_00ICALL_SAFE_STUB(void)
{
    /* Rate-limited: this used to fprintf+fflush on every call, which cost
     * 80k flushes in a spin and buried the log. The histogram above carries
     * the diagnostic value now. */
    static uint64_t hits;
    if (++hits <= 8 || (hits & 0xFFFFu) == 0) {
        /* Carry the running indirect-call total. How much code actually
         * executes is the signal that matters - the kernel-call count is a
         * narrow proxy that stayed near-flat (54->58) while total icalls went
         * 80 -> 60164. progress.py parses [ICALL-TOTAL]. */
        fprintf(stderr, "[SAFE_STUB] bad ICALL target -> safe no-op (#%llu)"
                        "  [ICALL-TOTAL] %llu\n",
                (unsigned long long)hits,
                (unsigned long long)g_icall_count);
        fflush(stderr);
    }
    /* Return 0, not a host pointer.
     *
     * This used to be `g_eax = (uint32_t)sub_00ICALL_SAFE_NOOP`, truncating a
     * 64-bit HOST function address into a 32-bit GUEST register. That value is
     * meaningless as an Xbox VA, and - worse - it moves every time code layout
     * shifts, so a build with one extra fprintf produced a different number.
     * The plausibility filter in RECOMP_ICALL_SAFE then judged it differently
     * build to build and the game took a different path: measured 58 kernel
     * calls versus 40 from a logging-only change, which made every
     * before/after comparison unreliable.
     *
     * 0 is deterministic, and the filter already rejects it explicitly
     * (_va < 0x00010000), so a caller that does `call eax` lands back here
     * instead of jumping somewhere layout-dependent. */
    g_eax = 0;
}
