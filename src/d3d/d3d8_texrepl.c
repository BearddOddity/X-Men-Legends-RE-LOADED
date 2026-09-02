/*
 * d3d8_texrepl.c - high-resolution texture replacement.  See the header.
 *
 * Design notes worth keeping:
 *
 * - Replacements are HOST memory. They never enter the guest address space, so
 *   none of the Xbox limits apply: not the 64 MB of RAM, not the payload
 *   arena's 256 MB ceiling, not the XDK's 28-bit resource pointers. This is
 *   the only place in the port where "modern PC budget" is literally true.
 *
 * - Identity is a content hash, not a filename. The game loads art out of .igb
 *   archives; hooking that loader would tie replacement to one asset pipeline
 *   and break the moment a texture arrives another way. Hashing what actually
 *   reaches the GPU is pipeline-independent.
 *
 * - Negative lookups are cached. A texture re-uploaded every frame would
 *   otherwise stat() a missing file every frame.
 */

#include "d3d8_texrepl.h"
#include "d3d8_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

unsigned g_texrepl_hits;
unsigned g_texrepl_misses;
uint64_t g_texrepl_vram_bytes;

static char g_dir[MAX_PATH];
static int  g_enabled = -1;          /* -1 = not yet probed */

/* Small open-addressed cache. A view of NULL with used=1 is a cached miss. */
#define TEXREPL_CACHE 512
typedef struct {
    uint64_t hash;
    ID3D11ShaderResourceView *srv;
    int used;
} TexReplEntry;
static TexReplEntry g_cache[TEXREPL_CACHE];

void d3d8_TexReplInit(void)
{
    const char *dir;
    DWORD attr;

    if (g_enabled >= 0)
        return;
    g_enabled = 0;

    dir = getenv("XBOX_TEXTURES");
    if (!dir || !*dir)
        return;

    attr = GetFileAttributesA(dir);
    if (attr == INVALID_FILE_ATTRIBUTES || !(attr & FILE_ATTRIBUTE_DIRECTORY)) {
        fprintf(stderr,
            "[TEXREPL] XBOX_TEXTURES=\"%s\" is not a directory - disabled\n", dir);
        fflush(stderr);
        return;
    }
    strncpy(g_dir, dir, sizeof(g_dir) - 1);
    g_dir[sizeof(g_dir) - 1] = '\0';
    g_enabled = 1;
    fprintf(stderr, "[TEXREPL] replacement textures from \"%s\"\n", g_dir);
    fflush(stderr);
}

int d3d8_TexReplEnabled(void)
{
    if (g_enabled < 0) d3d8_TexReplInit();
    return g_enabled == 1;
}

/*
 * FNV-1a over the image, walked row by row so padding between rows never
 * contributes - two uploads of the same picture at different pitches must
 * produce the same hash.
 */
uint64_t d3d8_TexHash(const void *pixels, uint32_t width, uint32_t height,
                      uint32_t pitch, uint32_t fmt)
{
    const uint8_t *p = (const uint8_t *)pixels;
    uint64_t h = 1469598103934665603ULL;      /* FNV-1a offset basis */
    uint32_t row_bytes, y, i;

    if (!p || !width || !height) return 0;

    /* Shape and format take part, so identical bytes in a different shape do
     * not collide onto one replacement. */
    {
        uint32_t meta[3] = { width, height, fmt };
        const uint8_t *m = (const uint8_t *)meta;
        for (i = 0; i < sizeof(meta); i++) {
            h ^= m[i];
            h *= 1099511628211ULL;
        }
    }

    row_bytes = pitch ? pitch : width * 4u;
    if (pitch && pitch < width * 4u) row_bytes = pitch;

    for (y = 0; y < height; y++) {
        const uint8_t *row = p + (size_t)y * row_bytes;
        for (i = 0; i < row_bytes; i++) {
            h ^= row[i];
            h *= 1099511628211ULL;
        }
    }
    return h;
}

/*
 * Minimal 32-bit uncompressed BMP reader.
 *
 * Deliberately not DDS: DDS needs either a parser for a large format or an
 * external library, and neither is worth it for a feature whose whole job is
 * "let someone drop in a bigger picture". BMP is exported by everything and is
 * about forty lines to read.
 *
 * Returns a malloc'd RGBA buffer top-down, or NULL.
 */
static uint8_t *texrepl_load_bmp(const char *path, uint32_t *out_w, uint32_t *out_h)
{
    FILE *f = fopen(path, "rb");
    uint8_t fh[14], ih[40];
    uint32_t data_off, w, hraw, row, y, x;
    int32_t  height_signed;
    uint16_t bpp;
    uint32_t compression;
    uint8_t *src = NULL, *dst = NULL;
    size_t src_size;
    int bottom_up;

    if (!f) return NULL;
    if (fread(fh, 1, sizeof(fh), f) != sizeof(fh) || fh[0] != 'B' || fh[1] != 'M')
        goto fail;
    if (fread(ih, 1, sizeof(ih), f) != sizeof(ih))
        goto fail;

    data_off      = (uint32_t)fh[10] | ((uint32_t)fh[11] << 8) |
                    ((uint32_t)fh[12] << 16) | ((uint32_t)fh[13] << 24);
    w             = (uint32_t)ih[4] | ((uint32_t)ih[5] << 8) |
                    ((uint32_t)ih[6] << 16) | ((uint32_t)ih[7] << 24);
    height_signed = (int32_t)((uint32_t)ih[8] | ((uint32_t)ih[9] << 8) |
                    ((uint32_t)ih[10] << 16) | ((uint32_t)ih[11] << 24));
    bpp           = (uint16_t)((uint16_t)ih[14] | ((uint16_t)ih[15] << 8));
    compression   = (uint32_t)ih[16] | ((uint32_t)ih[17] << 8) |
                    ((uint32_t)ih[18] << 16) | ((uint32_t)ih[19] << 24);

    bottom_up = height_signed > 0;
    hraw = (uint32_t)(height_signed > 0 ? height_signed : -height_signed);

    if (bpp != 32 || (compression != 0 && compression != 3) || !w || !hraw) {
        fprintf(stderr,
            "[TEXREPL] %s: need a 32-bit uncompressed BMP (got %u bpp, "
            "compression %u)\n", path, bpp, compression);
        goto fail;
    }

    /*
     * Only an upper bound is checked. Any size up to the limit is served -
     * 1024, 2048 and 4096 are the sizes actually verified - so replacement art
     * can arrive one resolution at a time. The ceiling is policy, not hardware:
     * D3D11 feature level 11 allows 16384, but one 4K replacement plus its mip
     * chain is roughly 85 MB of VRAM, so a few dozen would exhaust a mid-range
     * card for detail nobody can resolve at that density. Reject loudly here
     * rather than fail later at CreateTexture2D with a bare HRESULT.
     */
    if (w > TEXREPL_MAX_DIM || hraw > TEXREPL_MAX_DIM) {
        fprintf(stderr, "[TEXREPL] %s: %ux%u exceeds the %ux%u limit - skipped", path, w, hraw, TEXREPL_MAX_DIM, TEXREPL_MAX_DIM);
        fputc(10, stderr);
        goto fail;
    }

    row = w * 4u;
    src_size = (size_t)row * hraw;
    src = (uint8_t *)malloc(src_size);
    dst = (uint8_t *)malloc(src_size);
    if (!src || !dst) goto fail;

    if (fseek(f, (long)data_off, SEEK_SET) != 0) goto fail;
    if (fread(src, 1, src_size, f) != src_size) goto fail;

    /* BMP stores BGRA; DXGI_FORMAT_B8G8R8A8_UNORM matches, so only the row
     * order needs fixing. */
    for (y = 0; y < hraw; y++) {
        const uint8_t *s = src + (size_t)(bottom_up ? (hraw - 1 - y) : y) * row;
        memcpy(dst + (size_t)y * row, s, row);
    }
    (void)x;

    free(src);
    fclose(f);
    *out_w = w;
    *out_h = hraw;
    return dst;

fail:
    if (src) free(src);
    if (dst) free(dst);
    fclose(f);
    return NULL;
}

/*
 * Build a mip-mapped shader resource view from a level-0 image.
 *
 * Mips are not optional at these sizes. A 4096x4096 replacement standing in for
 * a 64x64 original is minified enormously, and without a mip chain every distant
 * surface aliases and crawls - it would look worse in motion than the texture it
 * replaced, which is the opposite of the point.
 *
 * That forces the creation path. A mip chain cannot be generated on an IMMUTABLE
 * texture created with initial data, so instead:
 *   MipLevels 0            let D3D11 size the full chain
 *   USAGE_DEFAULT          writable, so level 0 can be uploaded after creation
 *   BIND_RENDER_TARGET     GenerateMips renders the levels; it is required
 *   MISC_GENERATE_MIPS     and so is this
 * then upload level 0 and call GenerateMips.
 */
static ID3D11ShaderResourceView *texrepl_make_srv(const uint8_t *rgba,
                                                  uint32_t w, uint32_t h)
{
    D3D11_TEXTURE2D_DESC td;
    D3D11_SHADER_RESOURCE_VIEW_DESC svd;
    ID3D11Texture2D *tex = NULL;
    ID3D11ShaderResourceView *srv = NULL;
    ID3D11Device *dev = d3d8_GetD3D11Device();
    ID3D11DeviceContext *ctx = d3d8_GetD3D11Context();
    HRESULT hr;

    if (!dev || !ctx) return NULL;

    memset(&td, 0, sizeof(td));
    td.Width = w;
    td.Height = h;
    td.MipLevels = 0;                      /* full chain, sized by D3D11 */
    td.ArraySize = 1;
    td.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    td.SampleDesc.Count = 1;
    td.Usage = D3D11_USAGE_DEFAULT;
    td.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
    td.MiscFlags = D3D11_RESOURCE_MISC_GENERATE_MIPS;

    hr = ID3D11Device_CreateTexture2D(dev, &td, NULL, &tex);
    if (FAILED(hr)) {
        fprintf(stderr, "[TEXREPL] CreateTexture2D failed 0x%08lX (%ux%u)\n",
                (unsigned long)hr, w, h);
        return NULL;
    }

    memset(&svd, 0, sizeof(svd));
    svd.Format = td.Format;
    svd.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    svd.Texture2D.MipLevels = (UINT)-1;    /* every level the texture has */

    hr = ID3D11Device_CreateShaderResourceView(dev, (ID3D11Resource *)tex,
                                               &svd, &srv);
    if (FAILED(hr)) {
        fprintf(stderr, "[TEXREPL] CreateShaderResourceView failed 0x%08lX\n",
                (unsigned long)hr);
        ID3D11Texture2D_Release(tex);
        return NULL;
    }

    /* Level 0, then let the GPU filter the rest of the chain. */
    ID3D11DeviceContext_UpdateSubresource(ctx, (ID3D11Resource *)tex,
                                          0, NULL, rgba, w * 4u, 0);
    ID3D11DeviceContext_GenerateMips(ctx, srv);

    ID3D11Texture2D_Release(tex);          /* the view keeps it alive */

    /* A full chain costs about 4/3 of level 0. Tracked because a handful of
     * 4K replacements is real VRAM and silent exhaustion is hard to diagnose. */
    g_texrepl_vram_bytes += (uint64_t)w * h * 4ull * 4ull / 3ull;
    return srv;
}

ID3D11ShaderResourceView *d3d8_TexReplLookup(uint64_t hash,
                                             uint32_t width, uint32_t height,
                                             uint32_t fmt)
{
    char path[MAX_PATH];
    uint8_t *rgba;
    uint32_t rw = 0, rh = 0;
    size_t slot, probe;

    if (!d3d8_TexReplEnabled() || hash == 0)
        return NULL;

    slot = (size_t)(hash % TEXREPL_CACHE);
    for (probe = 0; probe < TEXREPL_CACHE; probe++) {
        TexReplEntry *e = &g_cache[(slot + probe) % TEXREPL_CACHE];
        if (!e->used) break;                 /* not seen before */
        if (e->hash == hash) {
            if (e->srv) g_texrepl_hits++;
            return e->srv;                   /* may be NULL: a cached miss */
        }
    }

    snprintf(path, sizeof(path), "%s\\%016llX.bmp", g_dir,
             (unsigned long long)hash);
    rgba = texrepl_load_bmp(path, &rw, &rh);

    {
        TexReplEntry *e = &g_cache[(slot + probe) % TEXREPL_CACHE];
        e->hash = hash;
        e->used = 1;
        e->srv  = rgba ? texrepl_make_srv(rgba, rw, rh) : NULL;
        if (rgba) free(rgba);

        if (e->srv) {
            g_texrepl_hits++;
            fprintf(stderr,
                "[TEXREPL] hit  %016llX  %ux%u -> %ux%u  (%.1fx, mips, %.1f MB VRAM)",
                (unsigned long long)hash, width, height, rw, rh,
                height ? (double)rh / (double)height : 0.0,
                (double)g_texrepl_vram_bytes / (1024.0 * 1024.0));
            fputc(10, stderr);
        } else {
            g_texrepl_misses++;
            /* The miss line is the interface: it names the file to provide. */
            if (g_texrepl_misses <= 64 || (g_texrepl_misses % 256) == 0)
                fprintf(stderr,
                    "[TEXREPL] miss %016llX  %ux%u fmt=%u\n",
                    (unsigned long long)hash, width, height, fmt);
        }
        fflush(stderr);
        return e->srv;
    }
}
