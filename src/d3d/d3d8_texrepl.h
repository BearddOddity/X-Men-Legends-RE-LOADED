/*
 * d3d8_texrepl.h - replace game textures with high-resolution art.
 *
 * The point of a PC port is not to be stuck with 2001 texture budgets. A
 * replacement never has to be visible to the game: the title keeps its own
 * small texture object and its own pixels, and the renderer quietly binds a
 * different shader resource view when it draws. So replacements live in plain
 * host memory, outside the guest address space, outside the payload arena, and
 * outside every Xbox size limit - there is no 64 MB, no 256 MB, no 28-bit
 * physical pointer involved.
 *
 * Identity is a content hash of the game's own level-0 pixels, taken at the
 * moment they are uploaded. Hashing the content rather than trusting an asset
 * name means no asset-loader hook is needed and the same texture is recognised
 * wherever it comes from.
 *
 * Enable by pointing at a directory:
 *
 *     set XBOX_TEXTURES=D:\My Games\Xbox Recomp\textures
 *
 * and put files in it named for the hash the log prints:
 *
 *     [TEXREPL] miss 3F2A9C41D0B7E856  64x64 fmt=6   <- dump this name
 *     textures\3F2A9C41D0B7E856.bmp                  <- provide this file
 *
 * 32-bit uncompressed BMP, which every image tool exports and which needs no
 * external library to read. Dimensions are free and need not be a multiple of
 * the original: a 64x64 texture may be served at 1024x1024, 2048x2048 or
 * 4096x4096. Only the upper bound below is enforced.
 *
 * Disabled unless XBOX_TEXTURES is set, so the default build is unaffected.
 */
#ifndef D3D8_TEXREPL_H
#define D3D8_TEXREPL_H

#include <stdint.h>

/* Before d3d11.h: without it the ID3D11Device_* names are not vtable macros
 * but implicit function calls, which compile and then fail at link. */
#ifndef COBJMACROS
#define COBJMACROS
#endif
#include <d3d11.h>

/** Read XBOX_TEXTURES. Safe to call more than once; only the first counts. */
void d3d8_TexReplInit(void);

/** True when a replacement directory was configured and exists. */
int d3d8_TexReplEnabled(void);

/**
 * Content hash of one level-0 image. Dimensions and format participate, so two
 * textures with identical bytes but different shapes stay distinct.
 */
uint64_t d3d8_TexHash(const void *pixels, uint32_t width, uint32_t height,
                      uint32_t pitch, uint32_t fmt);

/**
 * Shader resource view for a replacement, or NULL when there is none.
 *
 * Loaded once and cached, including the negative result - a texture uploaded
 * every frame must not hit the filesystem every frame. The returned view is
 * owned by this module and must not be released by the caller.
 */
ID3D11ShaderResourceView *d3d8_TexReplLookup(uint64_t hash,
                                             uint32_t width, uint32_t height,
                                             uint32_t fmt);

/*
 * Largest replacement accepted, per side. A policy limit, not a hardware one -
 * D3D11 feature level 11 allows 16384. A 4096x4096 replacement plus its
 * generated mip chain is roughly 85 MB of VRAM, so beyond this the memory buys
 * detail nobody can resolve.
 *
 * There is no lower bound: anything from the original size up to this limit is
 * accepted, so the ladder can be climbed one step at a time as art is produced.
 * Verified against a 64x64 original, with the VRAM one replacement costs:
 *
 *     512x512     1.3 MB      2048x2048    21.3 MB
 *     1024x1024   5.3 MB      4096x4096    85.3 MB
 *
 * Each step up is 4x the memory of the one before, which is why the ceiling
 * sits where it does rather than at what the hardware would allow.
 *
 * Every replacement gets a full mip chain. At these magnifications that is not
 * a nicety: a 4K texture standing in for a 64x64 original is minified hugely,
 * and unmipped it would alias and crawl in motion - worse than the texture it
 * replaced.
 */
#define TEXREPL_MAX_DIM 4096u

/** Counters for the run summary. */
extern unsigned g_texrepl_hits;
extern unsigned g_texrepl_misses;
extern uint64_t g_texrepl_vram_bytes;   /* level 0 plus generated mip chain */

#endif /* D3D8_TEXREPL_H */
