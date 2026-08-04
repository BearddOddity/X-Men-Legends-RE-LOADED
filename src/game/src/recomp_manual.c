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
extern void sub_00227F50(void);
extern void sub_00340CDE(void);
extern void sub_00340D86(void);
extern void sub_00343862(void);
extern void sub_00346743(void);
extern void sub_00349FAB(void);
extern void sub_0034AA86(void);
extern void sub_0034BB3A(void);
extern void sub_003556E0(void);

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
    if (xbox_va == 0x00227F50u) return sub_00227F50;
    if (xbox_va == 0x00340CDEu) return sub_00340CDE;
    if (xbox_va == 0x00340D86u) return sub_00340D86;
    if (xbox_va == 0x00343862u) return sub_00343862;
    if (xbox_va == 0x00346743u) return sub_00346743;
    if (xbox_va == 0x00349FABu) return sub_00349FAB;
    if (xbox_va == 0x0034AA86u) return sub_0034AA86;
    if (xbox_va == 0x0034BB3Au) return sub_0034BB3A;
    if (xbox_va == 0x003556E0u) return sub_003556E0;

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
