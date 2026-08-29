/**
 * Guest page-zero access census. See xbox_page_zero_trap.h for what this is
 * for, how to build it, and what it cannot see.
 */

#include "xbox_page_zero_trap.h"

#ifndef RECOMP_TRAP_PAGE_ZERO

void xbox_PageZeroTrapInit(void *memory_base) { (void)memory_base; }
void xbox_PageZeroTrapShutdown(void) { }

#else /* RECOMP_TRAP_PAGE_ZERO */

#include "platform/xbox_winnt.h"
#include <stdint.h>
#include <stdio.h>

#define PAGE_ZERO_SIZE      4096

/* Distinct faulting RIPs we will remember.
 *
 * The key is the RIP ALONE, deliberately. The first version of this keyed on
 * (rip, offset, direction) and a single scanning loop - one instruction
 * walking guest 0x600..0x700 - filled all 256 slots by itself, because every
 * dword it touched looked like a new site. What a reader needs is "which
 * instruction touches page 0", with the offset RANGE it covered; the offsets
 * are a property of the site, not separate sites. */
#define MAX_SITES           256

typedef struct {
    uintptr_t rip;
    uint32_t  first_offset; /* guest VA within page 0, first one seen */
    uint32_t  min_offset;
    uint32_t  max_offset;
    uint64_t  reads;
    uint64_t  writes;
} page_zero_site;

static uint8_t        *g_page0;          /* native address of guest VA 0 */
static PVOID           g_veh_handle;
static uintptr_t       g_image_base;
static int             g_armed;
static int             g_stepping;
static uint64_t        g_total_hits;
static uint64_t        g_dropped_sites;  /* distinct sites past MAX_SITES */
static page_zero_site  g_sites[MAX_SITES];
static int             g_site_count;

static void page_zero_protect(DWORD protect)
{
    DWORD old;
    VirtualProtect(g_page0, PAGE_ZERO_SIZE, protect, &old);
}

static void page_zero_record(uintptr_t rip, uint32_t offset, int is_write)
{
    int i;

    g_total_hits++;

    for (i = 0; i < g_site_count; i++) {
        if (g_sites[i].rip == rip) {
            if (is_write) g_sites[i].writes++; else g_sites[i].reads++;
            if (offset < g_sites[i].min_offset) g_sites[i].min_offset = offset;
            if (offset > g_sites[i].max_offset) g_sites[i].max_offset = offset;
            return;
        }
    }

    if (g_site_count >= MAX_SITES) {
        g_dropped_sites++;
        return;
    }

    g_sites[g_site_count].rip          = rip;
    g_sites[g_site_count].first_offset = offset;
    g_sites[g_site_count].min_offset   = offset;
    g_sites[g_site_count].max_offset   = offset;
    g_sites[g_site_count].reads        = is_write ? 0 : 1;
    g_sites[g_site_count].writes       = is_write ? 1 : 0;
    g_site_count++;

    /* Print each NEW site as it happens as well as in the summary, so a run
     * that crashes before shutdown still yields the census. */
    fprintf(stderr, "[PAGE0] site #%d: first %s of guest VA 0x%08X"
                    "  rip=0x%p  rva=0x%zX\n",
            g_site_count,
            is_write ? "WRITE" : "read",
            offset,
            (void *)rip,
            (size_t)(rip - g_image_base));
    fflush(stderr);
}

static LONG CALLBACK page_zero_handler(PEXCEPTION_POINTERS info)
{
    const EXCEPTION_RECORD *er = info->ExceptionRecord;

    if (!g_armed) {
        return EXCEPTION_CONTINUE_SEARCH;
    }

    if (er->ExceptionCode == EXCEPTION_ACCESS_VIOLATION &&
        er->NumberParameters >= 2) {
        uintptr_t addr = (uintptr_t)er->ExceptionInformation[1];

        if (addr >= (uintptr_t)g_page0 &&
            addr <  (uintptr_t)g_page0 + PAGE_ZERO_SIZE) {

            page_zero_record((uintptr_t)er->ExceptionAddress,
                             (uint32_t)(addr - (uintptr_t)g_page0),
                             er->ExceptionInformation[0] == 1);

            /* Let the faulting instruction complete, then re-arm. The trap
             * flag fires EXCEPTION_SINGLE_STEP after it retires. A string
             * instruction (rep movs) re-faults per re-armed step, which is
             * slow but correct - the site is deduplicated by count. */
            page_zero_protect(PAGE_READWRITE);
            g_stepping = 1;
            info->ContextRecord->EFlags |= 0x100;   /* TF */
            return EXCEPTION_CONTINUE_EXECUTION;
        }

        return EXCEPTION_CONTINUE_SEARCH;
    }

    if (er->ExceptionCode == EXCEPTION_SINGLE_STEP && g_stepping) {
        g_stepping = 0;
        page_zero_protect(PAGE_NOACCESS);
        return EXCEPTION_CONTINUE_EXECUTION;
    }

    return EXCEPTION_CONTINUE_SEARCH;
}

void xbox_PageZeroTrapInit(void *memory_base)
{
    if (g_armed || !memory_base) {
        return;
    }

    g_page0 = (uint8_t *)memory_base;
    g_image_base = (uintptr_t)GetModuleHandleA(NULL);

    /* First in the chain: this must see the fault before anything else
     * decides the process is dead. */
    g_veh_handle = AddVectoredExceptionHandler(1, page_zero_handler);
    if (!g_veh_handle) {
        fprintf(stderr, "[PAGE0] AddVectoredExceptionHandler failed (error %lu)"
                        " - census NOT armed\n", GetLastError());
        return;
    }

    {
        DWORD old = 0;
        if (!VirtualProtect(g_page0, PAGE_ZERO_SIZE, PAGE_NOACCESS, &old)) {
            fprintf(stderr, "[PAGE0] VirtualProtect(PAGE_NOACCESS) failed"
                            " (error %lu) - census NOT armed\n", GetLastError());
            RemoveVectoredExceptionHandler(g_veh_handle);
            g_veh_handle = NULL;
            return;
        }
    }

    g_armed = 1;
    fprintf(stderr, "[PAGE0] census ARMED - guest VA 0x00000000-0x00000FFF is"
                    " PAGE_NOACCESS at %p (image base 0x%zX)\n",
            (void *)g_page0, (size_t)g_image_base);
    fprintf(stderr, "[PAGE0] this is a DIAGNOSTIC build - do not measure"
                    " coverage or progress on it\n");
    fflush(stderr);
}

void xbox_PageZeroTrapShutdown(void)
{
    int i;

    if (!g_armed) {
        return;
    }

    g_armed = 0;
    page_zero_protect(PAGE_READWRITE);
    if (g_veh_handle) {
        RemoveVectoredExceptionHandler(g_veh_handle);
        g_veh_handle = NULL;
    }

    fprintf(stderr, "\n[PAGE0] ===== guest page-zero census =====\n");
    fprintf(stderr, "[PAGE0] %llu accesses across %d distinct sites\n",
            (unsigned long long)g_total_hits, g_site_count);

    if (g_site_count == 0) {
        fprintf(stderr, "[PAGE0] NOTHING touched guest page 0. Nothing in the"
                        " boot depends on it being mapped.\n");
    }

    for (i = 0; i < g_site_count; i++) {
        fprintf(stderr, "[PAGE0]   rva=0x%-9zX  %llu read / %llu write"
                        "  guest VA 0x%08X..0x%08X  rip=0x%p\n",
                (size_t)(g_sites[i].rip - g_image_base),
                (unsigned long long)g_sites[i].reads,
                (unsigned long long)g_sites[i].writes,
                g_sites[i].min_offset,
                g_sites[i].max_offset,
                (void *)g_sites[i].rip);
    }

    if (g_dropped_sites) {
        fprintf(stderr, "[PAGE0] WARNING: %llu further distinct sites were NOT"
                        " recorded (MAX_SITES = %d). This list is INCOMPLETE.\n",
                (unsigned long long)g_dropped_sites, MAX_SITES);
    }

    fprintf(stderr, "[PAGE0] resolve rva against build/*.map using the"
                    " Rva+Base column - see tools_data/triage_crash.py\n");
    fprintf(stderr, "[PAGE0] ===================================\n");
    fflush(stderr);
}

#endif /* RECOMP_TRAP_PAGE_ZERO */
