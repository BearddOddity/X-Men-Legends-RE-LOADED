/**
 * Xbox Memory Layout Compatibility
 *
 * The Xbox has 64MB of unified memory shared between CPU and GPU.
 * Memory is identity-mapped (physical == virtual for most of it).
 * Game code and data are linked to specific address ranges which vary
 * per game. Section addresses are parsed dynamically from the XBE header
 * at runtime, so this module works with ANY Xbox game.
 *
 * On Windows, we:
 * 1. Create a 64MB file mapping (CreateFileMapping)
 * 2. Map the base view + 28 mirror views at 64MB intervals
 * 3. Parse the XBE section table and copy sections to their Xbox VAs
 * 4. Set up simulated stack, heap, TIB, and kernel data area
 *
 * The mirror views ensure Xbox RAM wrapping works correctly: the Xbox
 * memory controller uses a 26-bit address bus, so ALL addresses wrap
 * modulo 64MB. File mapping views backed by the same section give us
 * true aliases where writes at one address are visible at all mirrors.
 */

#ifndef XBOX_MEMORY_LAYOUT_H
#define XBOX_MEMORY_LAYOUT_H

#include "platform/xbox_winnt.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ================================================================
 * Xbox memory map constants
 * ================================================================ */

/* Base address of all XBE files in Xbox memory */
#define XBOX_BASE_ADDRESS       0x00010000

/* Start of mapped region - includes low memory (KPCR at 0x0) because
 * game code reads from addresses like 0x20 and 0x28 (Xbox kernel structures). */
#define XBOX_MAP_START          0x00000000

/* Xbox physical memory - configurable, defaults to the retail 64 MB.
 *
 * Retail Xbox has 64 MB. Development kits shipped with 128 MB and the XDK
 * supported it, so 128 is a configuration the original code was written to
 * tolerate rather than one we invented.
 *
 *     set XBOX_RAM_MB=128
 *
 * Default is 64, so the baseline is unchanged unless you ask for more.
 *
 * How the game learns the size. It is NOT only MmQueryStatistics: the XDK
 * probes by walking memory and checking whether high addresses alias low ones.
 * The Xbox memory controller has a 26-bit address bus, so on retail hardware
 * everything wraps modulo 64 MB, and this port reproduces that with mirror
 * views (see xbox_MemoryLayoutInit). kernel_bridge.c records observing exactly
 * such a walk - "a linear 64 KB walk from 0x04000000".
 *
 * So raising the size means moving the mirror boundary as well as the reported
 * page count, and both follow g_xbox_total_ram automatically: the mirror loop
 * strides by g_memory_size, which is set from it.
 *
 * ONLY 64 AND 128 ARE ACCEPTED, for two reasons found in this codebase rather
 * than assumed:
 *   - kernel_rtl.c masks addresses with (XBOX_TOTAL_RAM - 1), which is only
 *     correct for a power of two.
 *   - The XDK stores D3D resource data pointers as 28-bit physical addresses
 *     (`ptr & 0x0FFFFFFF`, see d3d8_shim.c). 256 MB is exactly where that
 *     starts truncating silently, so it is not a safe next step.
 */
#define XBOX_TOTAL_RAM_DEFAULT  (64u * 1024u * 1024u)   /* retail  */
#define XBOX_TOTAL_RAM_DEVKIT   (128u * 1024u * 1024u)  /* devkit  */
extern uint32_t g_xbox_total_ram;
#define XBOX_TOTAL_RAM          g_xbox_total_ram
#define XBOX_GPU_RESERVED       (4 * 1024 * 1024)   /* ~4 MB for GPU */

/* NOTE: Section addresses (.text, .rdata, .data, etc.) are NOT hardcoded.
 * They are parsed from the XBE header at runtime in xbox_MemoryLayoutInit().
 * This allows the toolkit to work with ANY Xbox game without modification. */

/* ================================================================
 * Memory initialization
 * ================================================================ */

/**
 * Initialize the Xbox memory layout.
 *
 * Reserves the virtual address range 0x00010000 through 0x0076F000
 * and maps the XBE sections to their expected addresses:
 * - .rdata: copied from XBE, read-only
 * - .data: initialized portion copied from XBE, BSS zeroed
 *
 * Note: .text is NOT mapped here - the recompiled code is native
 * Windows code and doesn't need to be at the original address.
 * The data sections DO need to be at their original addresses
 * because the recompiled code references globals by absolute address.
 *
 * @param xbe_data  Pointer to the loaded XBE file contents.
 * @param xbe_size  Size of the XBE file.
 * @return TRUE on success, FALSE on failure.
 */
BOOL xbox_MemoryLayoutInit(const void *xbe_data, size_t xbe_size);

/**
 * Release the reserved Xbox memory layout.
 */
void xbox_MemoryLayoutShutdown(void);

/**
 * Check if an address falls within the Xbox memory map.
 */
BOOL xbox_IsXboxAddress(uintptr_t address);

/**
 * Get the base pointer for direct memory access.
 * Returns NULL if memory layout is not initialized.
 */
void *xbox_GetMemoryBase(void);

/**
 * Get the offset from Xbox VA to actual mapped address.
 * actual_address = xbox_va + offset
 * Returns 0 if memory is mapped at original Xbox addresses (ideal case).
 */
ptrdiff_t xbox_GetMemoryOffset(void);

/* ================================================================
 * Xbox stack for recompiled code
 * ================================================================ */

/* ================================================================
 * Kernel data export area
 * ================================================================ */

/** Base VA for kernel data exports (XboxHardwareInfo, XboxKrnlVersion, etc.)
 *  These are kernel exports that are DATA, not functions. The game reads
 *  their thunk entries and dereferences them to access the data. */
#define XBOX_KERNEL_DATA_BASE   0x00740000
#define XBOX_KERNEL_DATA_SIZE   4096   /* 4 KB - plenty for all data exports */

/* Offsets within the kernel data area */
#define KDATA_HARDWARE_INFO     0x000  /* XBOX_HARDWARE_INFO (8 bytes) */
#define KDATA_KRNL_VERSION      0x010  /* XBOX_KRNL_VERSION (8 bytes) */
#define KDATA_TICK_COUNT        0x020  /* KeTickCount (4 bytes) */
#define KDATA_LAUNCH_DATA_PAGE  0x030  /* LaunchDataPage (4 bytes, pointer) */
#define KDATA_THREAD_OBJ_TYPE   0x040  /* PsThreadObjectType (4 bytes) */
#define KDATA_EVENT_OBJ_TYPE    0x050  /* ExEventObjectType (4 bytes) */
#define KDATA_XE_IMAGE_FILENAME 0x060  /* XeImageFileName (ANSI_STRING) */
#define KDATA_IO_COMPLETION_TYPE 0x070 /* IoCompletionObjectType (4 bytes) */
#define KDATA_IO_DEVICE_TYPE    0x080  /* IoDeviceObjectType (4 bytes) */
#define KDATA_HD_KEY            0x100  /* XboxHDKey (16 bytes) */
#define KDATA_SIGNATURE_KEY     0x110  /* XboxSignatureKey (16 bytes) */
#define KDATA_LAN_KEY           0x120  /* XboxLANKey (16 bytes) */
#define KDATA_ALT_SIGNATURE_KEYS 0x130 /* XboxAlternateSignatureKeys (256 bytes) */
#define KDATA_XE_PUBLIC_KEY     0x300  /* XePublicKeyData (284 bytes) */

/* Thread-local qualifier for the simulated register file.
 *
 * The x86 register file belongs to a CPU thread, not to a process. Modelling
 * it as plain globals was only correct while the port had a single thread of
 * execution - which it did, because PsCreateSystemThreadEx ran every start
 * routine synchronously. Real threads are impossible until this is per-thread:
 * two threads sharing g_esp corrupt each other on the first push.
 *
 * EVERY declaration of these must carry the qualifier. main.c used to declare
 * its own copies without it; MSVC accepts the mismatch across translation
 * units and silently resolves the reference to the image's read-only TLS
 * template, so the first write faulted. That cost a full revert cycle.
 */
#ifndef RECOMP_TLS
#if defined(_MSC_VER)
#define RECOMP_TLS __declspec(thread)
#else
#define RECOMP_TLS _Thread_local
#endif
#endif

/** Size of the simulated Xbox stack (8 MB).
 *  Increased from 1 MB because failed RECOMP_ICALL indirect calls
 *  can leak stdcall args onto the stack each frame. An 8 MB stack
 *  provides enough headroom for extended gameplay sessions. */
#define XBOX_STACK_SIZE     (8 * 1024 * 1024)

/** Base VA of the stack area (above last XBE section). */
#define XBOX_STACK_BASE     0x00780000

/** Initial ESP value (top of stack, 16-byte aligned). */
#define XBOX_STACK_TOP      (XBOX_STACK_BASE + XBOX_STACK_SIZE - 16)

/* ================================================================
 * Xbox dynamic heap (for MmAllocateContiguousMemory, etc.)
 * ================================================================ */

/** Base VA of the dynamic heap area (above stack). */
#define XBOX_HEAP_BASE      (XBOX_STACK_BASE + XBOX_STACK_SIZE)  /* 0x00F80000 */

/** Size of the dynamic heap.
 *  Xbox has 64 MB total RAM. The total mapped region (data + stack + heap)
 *  must equal 64 MB so the RenderWare engine's memory probing stops at the
 *  correct boundary. On a real Xbox, probing past 64 MB causes a page fault
 *  that the engine catches via SEH to determine available memory. */
#define XBOX_HEAP_SIZE      (XBOX_TOTAL_RAM - XBOX_HEAP_BASE)  /* ~55.5 MB */

/** No static mirror/guard region. RAM mirror is handled via file mapping
 *  views that alias the same physical pages as the base 64 MB region. */
#define XBOX_MIRROR_SIZE    0
#define XBOX_GUARD_SIZE     0

/** Number of 64 MB mirror views to pre-map (covers 1.75 GB of address space). */
#define XBOX_NUM_MIRRORS    28

/* ================================================================
 * Payload arena - resource storage outside the Xbox heap
 * ================================================================
 *
 * The point of a PC port is not to inherit a 2001 memory budget. Textures,
 * vertex buffers and audio do not have to live in the emulated 64 MB just
 * because the console had no choice; only the small headers the game inspects
 * do. This region is where the payloads go.
 *
 * Placement is forced by two facts already established in this codebase:
 *
 *   - The XDK stores D3D resource data pointers as 28-bit physical addresses
 *     (`ptr & 0x0FFFFFFF`, see d3d8_shim.c). Anything above 256 MB truncates
 *     silently, so the arena must end at or below 0x10000000.
 *   - Everything from the top of RAM upward is already RAM mirror. The first
 *     alias, at one RAM-size above zero, is what the XDK's memory probe reads
 *     to conclude how much RAM exists - so the arena must not sit there.
 *
 * 0x0C000000..0x10000000 satisfies both: it is the highest 64 MB below the
 * 28-bit ceiling, and it leaves the first mirrors - the ones the probe walks -
 * untouched. Mirror views that would overlap it are skipped; the mapping code
 * already tolerates a mirror failing to map.
 *
 * Disabled by default. Set XBOX_PAYLOAD_MB to enable, e.g.
 *
 *     set XBOX_PAYLOAD_MB=64
 *
 * This is guest-addressable memory, so the game can lock a surface and write
 * pixels into it exactly as before. It is NOT Xbox RAM: the game's own
 * allocator never sees it and its heap accounting is unchanged.
 *
 * Assets the game never dereferences - replacement textures served straight to
 * D3D11 - do not belong here either. They need no guest address at all and
 * should live in plain host memory, where no Xbox limit applies.
 */
#define XBOX_PAYLOAD_BASE   0x0C000000u          /* 192 MB */
#define XBOX_PAYLOAD_LIMIT  0x10000000u          /* 256 MB - the 28-bit ceiling */
#define XBOX_PAYLOAD_MAX_MB 64u

extern uint32_t g_xbox_payload_size;   /* 0 when disabled */

/**
 * Allocate from the payload arena. Returns a guest VA, or 0 if the arena is
 * disabled or exhausted. Bump allocated and never freed: resource payloads
 * live for the process, and a real allocator here would be inventing a
 * lifetime the game does not express.
 */
uint32_t xbox_PayloadAlloc(uint32_t size, uint32_t align);

/**
 * Allocate from the Xbox heap. Returns an Xbox VA, or 0 on failure.
 * Alignment must be a power of 2 (minimum 4).
 * Thread-safe: no (single-threaded recompiled code).
 */
uint32_t xbox_HeapAlloc(uint32_t size, uint32_t alignment);
uint32_t xbox_HeapHighWater(void);

/**
 * Reserve at an EXACT address (NtAllocateVirtualMemory with a base hint).
 * Returns `base` on success, or 0 if it is below the high-water mark or the
 * block would run past the end of the heap. See ledger #75: the engine picks
 * its own base from a MEM_FREE scan and rejects any other address.
 */
uint32_t xbox_HeapAllocAt(uint32_t base, uint32_t size);

/**
 * Free a block from the Xbox heap. Currently a no-op (bump allocator).
 */
void xbox_HeapFree(uint32_t xbox_va);

/**
 * Get the file mapping handle for the Xbox memory region.
 * Used by the VEH handler to map additional mirror views on demand.
 * Returns NULL if file mapping is not available.
 */
HANDLE xbox_GetMappingHandle(void);

#ifdef __cplusplus
}
#endif

#endif /* XBOX_MEMORY_LAYOUT_H */
