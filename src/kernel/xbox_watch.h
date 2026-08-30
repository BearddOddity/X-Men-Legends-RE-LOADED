/**
 * Guest memory watchpoint - see xbox_watch.c for why and how.
 *
 * Compiles to nothing without RECOMP_WATCH_GUEST, so normal builds are
 * untouched. With it, the address to watch comes from the environment, so one
 * build answers many questions:
 *
 *     cmake -S . -B build_watch -DRECOMP_WATCH_GUEST=ON
 *     set RECOMP_WATCH=0x01098358+0x20
 *     build_watch\Release\xmen_legends_recomp.exe
 *
 * Answers "who wrote this?" including through memcpy, memset, the allocator,
 * or any computed pointer - all of which a grep for a store with the right
 * displacement cannot see.
 */
#ifndef XBOX_WATCH_H
#define XBOX_WATCH_H

#include <stdint.h>

/* Arm the watchpoint if RECOMP_WATCH names an address. Call after the guest
 * memory map exists and after setup has finished writing to it. */
void xbox_WatchInit(void *memory_base);

/* Disarm and print the hit count. Call before anything is unmapped. */
void xbox_WatchShutdown(void);

/* Compare the watched value against the last seen one and report a change,
 * naming `site` and printing a backtrace. Called after every recompiled call
 * in a RECOMP_WATCH_GUEST build. Catches writes the page watchpoint cannot -
 * memcpy, memset, and anything landing while the page is unprotected. */
void xbox_WatchPoll(const char *site);

/* The same, for an indirect call: names the target VA instead of a symbol. */
void xbox_WatchPollVA(uint32_t va);

#endif /* XBOX_WATCH_H */
