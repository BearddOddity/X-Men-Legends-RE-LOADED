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
    sub_001A3639();
    sub_001A23F3();
    sub_001A35AC();
    sub_001A3554();

    g_esp -= 4; MEM32(g_esp) = 0;
    g_esp -= 4; MEM32(g_esp) = 0;
    g_esp -= 4; MEM32(g_esp) = 0;
    g_esp -= 4; MEM32(g_esp) = 0;  /* dummy return address */
    sub_00011E40();
    g_esp += 16;

    g_esp -= 4; MEM32(g_esp) = 0;
    g_esp -= 4; MEM32(g_esp) = 1;
    g_esp -= 4; MEM32(g_esp) = 1;
    g_esp -= 4; MEM32(g_esp) = 0;  /* dummy return address */
    sub_001A237D();
    g_esp += 16;

    g_eax = 0;
}

recomp_func_t recomp_lookup_manual(uint32_t xbox_va)
{
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
    fflush(stderr);
}
