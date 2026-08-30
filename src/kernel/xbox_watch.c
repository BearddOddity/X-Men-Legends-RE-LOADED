/**
 * Guest memory watchpoint (opt-in debug build).
 *
 * WHY THIS EXISTS
 *
 * "Who wrote this value?" is answered on this project by grepping the generated
 * C for a store with the right displacement. That only finds a literal
 * `MEM32(reg + 0xNN) = ...`. It is blind to a memcpy, a memset, an allocator
 * scribbling a freed block, a write through a computed pointer, or a field
 * written via a different base register - and on 2026-08-29 that blindness
 * produced a confident wrong answer: a scan for writes of -1 to `[desc+0x20]`
 * found none, and "nothing writes it, so it is heap residue" went into the
 * notes as established when it was merely unrefuted.
 *
 * A hardware-style watchpoint cannot be fooled that way. It sees the write
 * whatever produced it.
 *
 * WHY IT IS DRIVEN BY AN ENVIRONMENT VARIABLE
 *
 * The probe workflow costs a full rebuild per question - two to three minutes
 * to learn one field value. Reading the address from the environment turns that
 * into one build and as many questions as you like:
 *
 *     set RECOMP_WATCH=0x01098358+0x20
 *     run.bat
 *
 * Accepts `0xVA`, `0xVA+0xOFF`, or `0xVA:LEN` (default length 4).
 *
 * WHAT IT PRINTS
 *
 * Every write into the watched range: the guest address, the value before and
 * after, the faulting host RIP and its RVA, and a native backtrace. Resolve the
 * RVAs with tools_data/triage_crash.py.
 *
 * HOW IT WORKS
 *
 * The same mechanism as xbox_page_zero_trap.c, which is the proven one in this
 * tree: protect the page, catch the access in a vectored handler, unprotect,
 * set the trap flag, re-protect on the single-step. Page granularity means
 * unrelated addresses on the same page also fault; they are filtered out and
 * resumed, so the cost is speed, not correctness.
 *
 * DIAGNOSTIC ONLY. It changes timing and it is slow. Never measure coverage or
 * progress on this build - use a separate build directory, the way build_abi
 * and build_page0 do.
 */
#include "xbox_watch.h"

#ifndef RECOMP_WATCH_GUEST

void xbox_WatchInit(void *memory_base) { (void)memory_base; }
void xbox_WatchPoll(const char *site) { (void)site; }
void xbox_WatchPollVA(uint32_t va) { (void)va; }
void xbox_WatchShutdown(void) { }

#else

#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define WATCH_FRAMES 12

static uint8_t  *g_base;          /* native address of guest VA 0 */
static uint8_t  *g_watch;         /* native address of the watched range */
static uint32_t  g_watch_va;      /* guest VA being watched */
static uint32_t  g_watch_len;
static uint8_t  *g_page;          /* page containing it */
static SIZE_T    g_page_len;
static PVOID     g_veh;
static uintptr_t g_image_base;
static int       g_armed;
static int       g_stepping;
static uint64_t  g_hits;
static uint32_t  g_last_value;
static uint32_t  g_poll_last;
static uint64_t  g_polls;

static void watch_protect(DWORD protect)
{
    DWORD old;
    VirtualProtect(g_page, g_page_len, protect, &old);
}

/* Parse RECOMP_WATCH: "0xVA", "0xVA+0xOFF", or "0xVA:LEN". */
static int watch_parse(const char *spec, uint32_t *va, uint32_t *len)
{
    char *end;
    unsigned long long base = strtoull(spec, &end, 0);
    unsigned long long off = 0, n = 4;

    if (end == spec)
        return 0;
    if (*end == '+') {
        off = strtoull(end + 1, &end, 0);
    } else if (*end == ':') {
        n = strtoull(end + 1, &end, 0);
        if (n == 0) n = 4;
    }
    *va = (uint32_t)(base + off);
    *len = (uint32_t)n;
    return 1;
}

static void watch_report(uintptr_t rip, uintptr_t addr, int is_write)
{
    uint32_t guest = (uint32_t)(addr - (uintptr_t)g_base);
    uint32_t now = 0;

    if (g_watch_len >= 4)
        memcpy(&now, g_watch, 4);

    g_hits++;
    fprintf(stderr,
            "[WATCH] #%llu %s guest 0x%08X  value %08X -> %08X  rip=0x%llX rva=0x%llX\n",
            (unsigned long long)g_hits, is_write ? "WRITE" : "read ", guest,
            g_last_value, now,
            (unsigned long long)rip,
            (unsigned long long)(rip - g_image_base));

    void *frames[WATCH_FRAMES];
    USHORT got = CaptureStackBackTrace(1, WATCH_FRAMES, frames, NULL);
    for (USHORT i = 0; i < got; i++) {
        uintptr_t a = (uintptr_t)frames[i];
        /* Only frames inside this module resolve against the map. */
        if (a >= g_image_base && a < g_image_base + 0x08000000ull)
            fprintf(stderr, "    [%2u] RVA 0x%llX\n", i,
                    (unsigned long long)(a - g_image_base));
    }
    fflush(stderr);
    g_last_value = now;
}

static LONG CALLBACK watch_handler(PEXCEPTION_POINTERS info)
{
    PEXCEPTION_RECORD er = info->ExceptionRecord;

    if (er->ExceptionCode == EXCEPTION_ACCESS_VIOLATION && g_armed) {
        uintptr_t addr = (uintptr_t)er->ExceptionInformation[1];

        if (addr >= (uintptr_t)g_page && addr < (uintptr_t)g_page + g_page_len) {
            /* Page granularity: only report hits inside the watched range,
             * but resume either way so the run is not disturbed. */
            if (addr >= (uintptr_t)g_watch &&
                addr < (uintptr_t)g_watch + g_watch_len) {
                watch_protect(PAGE_READWRITE);
                watch_report((uintptr_t)info->ContextRecord->Rip, addr,
                             er->ExceptionInformation[0] == 1);
            } else {
                watch_protect(PAGE_READWRITE);
            }
            /* Let the faulting instruction retire, then re-arm on the
             * single-step it raises. String instructions retire fully. */
            g_stepping = 1;
            info->ContextRecord->EFlags |= 0x100;   /* TF */
            return EXCEPTION_CONTINUE_EXECUTION;
        }
    }

    if (er->ExceptionCode == EXCEPTION_SINGLE_STEP && g_stepping) {
        g_stepping = 0;
        watch_protect(PAGE_READONLY);
        return EXCEPTION_CONTINUE_EXECUTION;
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

void xbox_WatchInit(void *memory_base)
{
    const char *spec = getenv("RECOMP_WATCH");
    SYSTEM_INFO si;

    if (!spec || !*spec)
        return;                       /* built in, but not asked for */

    if (!watch_parse(spec, &g_watch_va, &g_watch_len)) {
        fprintf(stderr, "[WATCH] cannot parse RECOMP_WATCH=\"%s\" - expected "
                        "0xVA, 0xVA+0xOFF or 0xVA:LEN\n", spec);
        fflush(stderr);
        return;
    }

    g_base = (uint8_t *)memory_base;
    g_watch = g_base + g_watch_va;
    g_image_base = (uintptr_t)GetModuleHandleW(NULL);

    GetSystemInfo(&si);
    g_page = (uint8_t *)((uintptr_t)g_watch & ~(uintptr_t)(si.dwPageSize - 1));
    g_page_len = si.dwPageSize;

    g_veh = AddVectoredExceptionHandler(1, watch_handler);
    if (!g_veh) {
        fprintf(stderr, "[WATCH] AddVectoredExceptionHandler failed (%lu)\n",
                GetLastError());
        fflush(stderr);
        return;
    }

    if (g_watch_len >= 4) {
        memcpy(&g_last_value, g_watch, 4);
        g_poll_last = g_last_value;
    }

    DWORD old;
    if (!VirtualProtect(g_page, g_page_len, PAGE_READONLY, &old)) {
        fprintf(stderr, "[WATCH] VirtualProtect failed (%lu)\n", GetLastError());
        fflush(stderr);
        RemoveVectoredExceptionHandler(g_veh);
        g_veh = NULL;
        return;
    }

    g_armed = 1;
    fprintf(stderr, "[WATCH] armed on guest 0x%08X..0x%08X (native %p), "
                    "initial value %08X\n",
            g_watch_va, g_watch_va + g_watch_len - 1, (void *)g_watch,
            g_last_value);
    fprintf(stderr, "[WATCH] this is a DIAGNOSTIC build - do not measure "
                    "coverage or progress on it\n");
    fflush(stderr);
}

/*
 * Software poll, called after every recompiled call when RECOMP_WATCH is set.
 *
 * The page-protection watchpoint above cannot see a write that lands while the
 * page is unprotected - which it must be while a faulting instruction retires -
 * and on 2026-08-29 it missed the write that mattered twice, reporting MISSED
 * WRITES both times. This is the blunt instrument that cannot be fooled: read
 * the value after every call and report when it changes. It names the callee
 * that changed it, which is the question being asked.
 *
 * Slow by construction. Diagnostic builds only.
 */
void xbox_WatchPoll(const char *site)
{
    uint32_t now;

    if (!g_watch || g_watch_len < 4)
        return;
    memcpy(&now, g_watch, 4);
    if (now == g_poll_last)
        return;

    g_polls++;
    fprintf(stderr, "[WATCH-POLL] #%llu guest 0x%08X changed %08X -> %08X "
                    "across %s\n",
            (unsigned long long)g_polls, g_watch_va, g_poll_last, now, site);
    g_poll_last = now;

    void *frames[WATCH_FRAMES];
    USHORT got = CaptureStackBackTrace(1, WATCH_FRAMES, frames, NULL);
    for (USHORT i = 0; i < got; i++) {
        uintptr_t a = (uintptr_t)frames[i];
        if (a >= g_image_base && a < g_image_base + 0x08000000ull)
            fprintf(stderr, "    [%2u] RVA 0x%llX\n", i,
                    (unsigned long long)(a - g_image_base));
    }
    fflush(stderr);
}

/* Same poll, for an indirect call, which has no symbol - name the target VA.
 * sub_002235D0 reached the write through one of these, and a poll on direct
 * calls alone attributed it to the whole driver. */
void xbox_WatchPollVA(uint32_t va)
{
    char site[32];
    _snprintf(site, sizeof site, "icall sub_%08X", va);
    site[sizeof site - 1] = 0;
    xbox_WatchPoll(site);
}

void xbox_WatchShutdown(void)
{
    if (!g_armed)
        return;
    g_armed = 0;
    watch_protect(PAGE_READWRITE);
    if (g_veh) {
        RemoveVectoredExceptionHandler(g_veh);
        g_veh = NULL;
    }
    uint32_t final = 0;
    if (g_watch_len >= 4)
        memcpy(&final, g_watch, 4);
    /* Print what we last SAW written next to what is actually there. If they
     * differ, writes were missed: protection is per-page, and the faulting
     * instruction runs with the page writable, so anything else writing in
     * that window is invisible. Say so rather than trusting the log. */
    fprintf(stderr, "[WATCH] guest 0x%08X: %llu access(es) reported, "
                    "last seen %08X, actually %08X%s\n",
            g_watch_va, (unsigned long long)g_hits, g_last_value, final,
            (g_last_value != final) ? "  <-- MISSED WRITES" : "");
    fflush(stderr);
}

#endif /* RECOMP_WATCH_GUEST */
