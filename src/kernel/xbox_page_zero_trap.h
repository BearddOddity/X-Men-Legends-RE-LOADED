/**
 * Guest page-zero access census (opt-in debug build).
 *
 * Guest VA 0 is mapped and zero-filled in the normal build, so a null
 * pointer dereference in lifted code does not fault - it quietly returns 0,
 * or returns whatever an earlier null-derived write left there. Several walls
 * on this project stayed invisible for exactly that reason:
 *
 *   - ledger #84/#85: sub_0021ACD0 walked a NULL object for a whole loop,
 *     writing ebp to guest VA 0xC and reading it back on the next iteration
 *     as if it were a live object.
 *   - ledger #88: sub_00202B87's allocator-owner lookup returned NULL and the
 *     code did MEM32(0) to fetch an icall target, getting 0 instead of a
 *     fault.
 *   - ledger #92: two guards in sub_00200B18 zero a bad pointer and then
 *     dereference it, reading guest VA 0x12 and 0x28.
 *
 * This module makes guest page 0 PAGE_NOACCESS and installs a vectored
 * exception handler that CENSUSES every access to it: guest offset, whether
 * it was a read or a write, and the faulting RIP (plus its RVA, for
 * tools_data/triage_crash.py). It then resumes the run rather than killing
 * it, so one boot yields the whole list instead of only the first hit.
 *
 * Build it with:
 *
 *     cmake -S . -B build_page0 -DRECOMP_TRAP_PAGE_ZERO=ON
 *
 * in a SEPARATE build directory, the same way RECOMP_CHECK_ABI is used.
 * Without the option this file compiles to nothing.
 *
 * Why this is safe now and was not before: the fake TIB used to live at guest
 * 0, because the lifter dropped fs: prefixes and fs:[n] became MEM32(n). It
 * was moved to 0x00770000 when fs: started lifting to MEM32(g_fs_base + n) -
 * see the long comment in xbox_memory_layout.c. Nothing is *known* to need
 * guest page 0 any more, and this census is how that gets checked rather than
 * assumed.
 *
 * LIMITATIONS, stated so a silent result is not mistaken for a clean one:
 *   - Only the BASE view's page 0 is protected. The 64 MB RAM mirrors alias
 *     the same physical page at their own offset 0, and an access that wraps
 *     into a mirror will not trap.
 *   - The resume path (unprotect, single-step, re-protect) assumes one
 *     thread is running lifted code. With real threads, two threads faulting
 *     at once could race the window where the page is readable.
 *   - This is a diagnostic. It changes timing and it must never be the build
 *     a result is measured on.
 */

#ifndef XBOX_PAGE_ZERO_TRAP_H
#define XBOX_PAGE_ZERO_TRAP_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Arm the census. Call once, AFTER the memory layout is fully built - the
 * layout's own initialisation writes to low memory and must not be trapped.
 *
 * @param memory_base  Base of the mapped 64 MB view, i.e. guest VA 0.
 * No-op unless RECOMP_TRAP_PAGE_ZERO is defined.
 */
void xbox_PageZeroTrapInit(void *memory_base);

/**
 * Disarm the census and print the summary table.
 * No-op unless RECOMP_TRAP_PAGE_ZERO is defined.
 */
void xbox_PageZeroTrapShutdown(void);

#ifdef __cplusplus
}
#endif

#endif /* XBOX_PAGE_ZERO_TRAP_H */
