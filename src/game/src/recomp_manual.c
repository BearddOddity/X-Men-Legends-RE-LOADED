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

/* ── Seeded functions (tools_data/seed_missing_functions.py) ── */
/*
 * Reachable only via data-section pointers, so function discovery
 * missed them and every indirect call here returned NULL. Declared
 * here rather than in the generated recomp_funcs.h so the wiring
 * survives a regeneration.
 */
extern void sub_002370B0(void);

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
    if (xbox_va == 0x002370B0u) return sub_002370B0;

    /* Video playback shim overrides - Phase 1: stub to return success immediately */
    if (xbox_va == 0x00340FEB) return sub_00340FEB;
    if (xbox_va == 0x003432A8) return sub_003432A8;
    if (xbox_va == 0x003464F1) return sub_003464F1;
    if (xbox_va == 0x003467B6) return sub_003467B6;
    if (xbox_va == 0x003467F2) return sub_003467F2;

    /* Network fallback override - stub to prevent blocking in main thread */
    if (xbox_va == 0x00345AB0) return sub_00345AB0;

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
void sub_00ICALL_SAFE_STUB(void)
{
    fprintf(stderr, "[SAFE_STUB] Called for bad ICALL target - redirecting to safe no-op\n");
    fflush(stderr);
    g_eax = (uint32_t)sub_00ICALL_SAFE_NOOP;  /* Redirect to safe no-op function */
}
