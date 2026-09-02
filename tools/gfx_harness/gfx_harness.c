/*
 * gfx_harness.c - drive the graphics runtime with no game attached.
 *
 * ============================================================================
 *  WHY THIS EXISTS
 *
 *  src/d3d, src/nv2a and src/apu are ~29,000 lines that have never faced a
 *  workload, because the boot dies in static initialiser 3 of 10 and all of it
 *  sits downstream. The scout build (RECOMP_SCOUT) established that surviving
 *  those initialisers does NOT reach rendering either - skipping a broken
 *  initialiser skips the objects the game loop needs, so no loop ever starts
 *  and not one D3D call is made.
 *
 *  So the graphics runtime cannot be reached through the game at all right
 *  now. This reaches it directly instead: no guest code, no initialisers, no
 *  recompiled functions. Just the host-side runtime, called from a plain main.
 *
 *  It answers one question: does any of it produce a frame?
 * ============================================================================
 *
 * Four stages, each reported independently, each survivable so a failure in
 * one still lets the later ones be measured:
 *
 *   1  create the D3D8 device      - does the renderer initialise at all?
 *   2  clear + present             - do frames reach a window?
 *   3  draw through the D3D8 path  - does geometry submit?
 *   4  drive the NV2A push buffer  - does pushbuffer -> pgraph -> d3d8 work?
 *
 * Stage 4 is the one that matters most, because it is the path the game
 * actually uses. src/nv2a/nv2a_pb_test.c already generates and dispatches a
 * frame of commands; nothing ever called it. This calls it.
 *
 * This is a test harness, not part of the port. It proves nothing about
 * whether the game works - only whether the runtime it depends on is alive.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include "d3d8_xbox.h"
#include "nv2a_state.h"
#include "nv2a_pgraph_d3d11.h"
#include "d3d8_texrepl.h"

/*
 * The guest-address base the runtime uses. The game defines this in
 * src/game/src/main.c, which the harness deliberately does not link - so the
 * harness defines it and owns it. No XBE is loaded; guest VAs resolve into a
 * block we allocate in stage 4.
 */
ptrdiff_t g_xbox_mem_offset = 0;

void d3d8_PresentFrame(void);
HWND d3d8_GetHWND(void);
void nv2a_pb_test_frame(void);
void nv2a_pb_test_set_active(int active);
HRESULT d3d8_CreateTextureImpl(UINT Width, UINT Height, UINT Levels,
                               DWORD Usage, D3DFORMAT Format,
                               IDirect3DTexture8 **ppTex);

/* Push buffer pointers, at the guest addresses nv2a_pb_test.c reads. */
#define PB_BASE_ADDR   0x35D69Cu
#define PB_WRITE_ADDR  0x35D6A0u
#define PB_END_ADDR    0x35D6A4u

/* Where the harness puts the push buffer inside its fake guest memory. */
#define PB_BUFFER_VA   0x00400000u
#define PB_BUFFER_SIZE 0x00010000u

#define GUEST_SIZE     (64u * 1024u * 1024u)

static int g_stage_ok[6];

#define REPORT(stage, ok, fmt, ...)                                          \
    do {                                                                     \
        g_stage_ok[stage] = (ok);                                            \
        fprintf(stderr, "[HARNESS] stage %d %-8s " fmt "\n",                 \
                (stage), (ok) ? "PASS" : "FAIL", __VA_ARGS__);               \
        fflush(stderr);                                                      \
    } while (0)

/* Pump the window queue so the OS does not mark us unresponsive and so a
 * window that is created actually paints. */
static void pump(void)
{
    MSG msg;
    while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }
}

int main(int argc, char **argv)
{
    int frames = (argc > 1) ? atoi(argv[1]) : 120;
    if (frames < 1) frames = 1;

    fprintf(stderr,
        "[HARNESS] ============================================\n"
        "[HARNESS] graphics runtime harness - no game attached\n"
        "[HARNESS] %d frames per stage\n"
        "[HARNESS] ============================================\n", frames);
    fflush(stderr);

    /* ---- stage 1: create the device ------------------------------------ */

    IDirect3D8 *d3d = xbox_Direct3DCreate8(220);
    if (!d3d) {
        REPORT(1, 0, "%s", "xbox_Direct3DCreate8 returned NULL");
        return 1;
    }

    D3DPRESENT_PARAMETERS pp;
    memset(&pp, 0, sizeof(pp));
    pp.BackBufferWidth  = 640;
    pp.BackBufferHeight = 480;
    pp.BackBufferFormat = D3DFMT_A8R8G8B8;
    pp.BackBufferCount  = 1;
    pp.SwapEffect       = D3DSWAPEFFECT_DISCARD;
    pp.Windowed         = TRUE;
    pp.EnableAutoDepthStencil = TRUE;
    pp.AutoDepthStencilFormat = D3DFMT_D24S8;

    IDirect3DDevice8 *dev = NULL;
    /* The Xbox header defines no D3DDEVTYPE_* - the console has one device
     * type - and CreateDevice takes a plain DWORD. 1 is D3DDEVTYPE_HAL's
     * value on the PC and is what the shim ignores anyway. */
    HRESULT hr = d3d->lpVtbl->CreateDevice(d3d, 0, 1 /* HAL */, NULL,
                                           0x00000040 /* SW vertex proc */,
                                           &pp, &dev);
    if (FAILED(hr) || !dev) {
        REPORT(1, 0, "CreateDevice hr=0x%08lX dev=%p", (unsigned long)hr, (void *)dev);
        return 1;
    }
    REPORT(1, 1, "device=%p hwnd=%p", (void *)dev, (void *)d3d8_GetHWND());

    /* ---- stage 2: clear and present ------------------------------------ */
    {
        int presented = 0;
        for (int i = 0; i < frames; i++) {
            /* Cycle the clear colour so a live window is obviously updating
             * rather than showing one static fill. */
            DWORD c = 0xFF000000u | ((i * 2) & 0xFF) << 16 | ((i * 3) & 0xFF) << 8 | 0x40;
            HRESULT ch = IDirect3DDevice8_Clear(dev, 0, NULL, 0x00000001 /* TARGET */,
                                                c, 1.0f, 0);
            if (FAILED(ch)) {
                REPORT(2, 0, "Clear failed on frame %d hr=0x%08lX", i, (unsigned long)ch);
                break;
            }
            d3d8_PresentFrame();
            presented++;
            pump();
        }
        if (presented == frames)
            REPORT(2, 1, "%d frames cleared and presented", presented);
        else if (!g_stage_ok[2])
            REPORT(2, 0, "only %d of %d frames presented", presented, frames);
    }

    /* ---- stage 3: submit geometry through the D3D8 path ----------------- */
    {
        /* One untransformed-ish triangle, XYZRHW|DIFFUSE so no shader or
         * transform state is required for it to be meaningful. */
        struct { float x, y, z, rhw; DWORD color; } tri[3] = {
            { 320.0f,  60.0f, 0.5f, 1.0f, 0xFFFF3020 },
            { 560.0f, 420.0f, 0.5f, 1.0f, 0xFF20FF40 },
            {  80.0f, 420.0f, 0.5f, 1.0f, 0xFF3040FF },
        };
        int drew = 0;
        for (int i = 0; i < frames; i++) {
            IDirect3DDevice8_Clear(dev, 0, NULL, 0x00000001, 0xFF101018, 1.0f, 0);
            HRESULT bh = dev->lpVtbl->BeginScene(dev);
            if (FAILED(bh)) { REPORT(3, 0, "BeginScene hr=0x%08lX", (unsigned long)bh); break; }
            dev->lpVtbl->SetVertexShader(dev, 0x004 | 0x040 /* XYZRHW|DIFFUSE */);
            HRESULT dh = dev->lpVtbl->DrawPrimitiveUP(dev, D3DPT_TRIANGLELIST, 1,
                                                      tri, sizeof(tri[0]));
            if (FAILED(dh)) { REPORT(3, 0, "DrawPrimitiveUP hr=0x%08lX", (unsigned long)dh); break; }
            dev->lpVtbl->EndScene(dev);
            d3d8_PresentFrame();
            drew++;
            pump();
        }
        if (drew == frames)
            REPORT(3, 1, "%d frames with a triangle submitted", drew);
        else if (!g_stage_ok[3])
            REPORT(3, 0, "only %d of %d frames drew", drew, frames);
    }

    /* ---- stage 4: drive the NV2A push buffer --------------------------- */
    {
        /* No XBE is loaded, so there is no guest memory. Make some: guest VA v
         * resolves to base + v, which is the same contract main.c sets up. */
        uint8_t *guest = (uint8_t *)VirtualAlloc(NULL, GUEST_SIZE,
                                                 MEM_COMMIT | MEM_RESERVE,
                                                 PAGE_READWRITE);
        uint8_t *vram  = (uint8_t *)VirtualAlloc(NULL, 64u * 1024u * 1024u,
                                                 MEM_COMMIT | MEM_RESERVE,
                                                 PAGE_READWRITE);
        uint8_t *ramin = (uint8_t *)VirtualAlloc(NULL, 1u * 1024u * 1024u,
                                                 MEM_COMMIT | MEM_RESERVE,
                                                 PAGE_READWRITE);
        if (!guest || !vram || !ramin) {
            REPORT(4, 0, "%s", "VirtualAlloc failed for guest/vram/ramin");
            goto summary;
        }
        g_xbox_mem_offset = (ptrdiff_t)guest;

        NV2AState *gpu = nv2a_init_standalone(vram, 64u * 1024u * 1024u,
                                              ramin, 1u * 1024u * 1024u);
        if (!gpu || !nv2a_get_state()) {
            REPORT(4, 0, "nv2a_init_standalone returned %p", (void *)gpu);
            goto summary;
        }
        pgraph_d3d11_init();

        /* Point the push buffer registers at our scratch block. */
        *(volatile uint32_t *)(guest + PB_BASE_ADDR)  = PB_BUFFER_VA;
        *(volatile uint32_t *)(guest + PB_WRITE_ADDR) = PB_BUFFER_VA;
        *(volatile uint32_t *)(guest + PB_END_ADDR)   = PB_BUFFER_VA + PB_BUFFER_SIZE;

        nv2a_pb_test_set_active(1);

        for (int i = 0; i < frames; i++) {
            IDirect3DDevice8_Clear(dev, 0, NULL, 0x00000001, 0xFF001018, 1.0f, 0);
            dev->lpVtbl->BeginScene(dev);
            nv2a_pb_test_frame();      /* generate + dispatch through pgraph */
            dev->lpVtbl->EndScene(dev);
            d3d8_PresentFrame();
            pump();
        }

        /* pgraph_d3d11_shutdown prints draws= and verts=, which is the number
         * that actually answers the question. */
        pgraph_d3d11_shutdown();
        REPORT(4, 1, "%d push-buffer frames dispatched (see PGRAPH-D3D11 "
                     "draws=/verts= above)", frames);
    }

    /* ---- stage 5: texture replacement --------------------------------- */
    {
        /*
         * Exercises the replacement path end to end with no game involved:
         * build a texture, upload known pixels, and see whether the hash and
         * lookup fire. This is the only part of the graphics work that can be
         * verified today, so it is worth verifying properly.
         *
         * A miss is a PASS. The point of the stage is that hashing and lookup
         * run and report the filename to supply; whether a replacement exists
         * on this machine is the user's business, not a test result.
         */
        IDirect3DTexture8 *tex = NULL;
        HRESULT th = d3d8_CreateTextureImpl(64, 64, 1, 0, D3DFMT_A8R8G8B8, &tex);
        if (FAILED(th) || !tex) {
            REPORT(5, 0, "CreateTextureImpl hr=0x%08lX", (unsigned long)th);
        } else {
            D3DLOCKED_RECT lr;
            memset(&lr, 0, sizeof(lr));
            if (SUCCEEDED(tex->lpVtbl->LockRect(tex, 0, &lr, NULL, 0)) && lr.pBits) {
                /* A deterministic gradient, so the hash is stable run to run
                 * and the name printed is the name to create. */
                for (int y = 0; y < 64; y++) {
                    unsigned char *row = (unsigned char *)lr.pBits + y * lr.Pitch;
                    for (int x = 0; x < 64; x++) {
                        row[x * 4 + 0] = (unsigned char)(x * 4);
                        row[x * 4 + 1] = (unsigned char)(y * 4);
                        row[x * 4 + 2] = 0x80;
                        row[x * 4 + 3] = 0xFF;
                    }
                }
                tex->lpVtbl->UnlockRect(tex, 0);   /* upload + replacement hook */
                REPORT(5, 1, "64x64 uploaded; texrepl %s (%u hit, %u miss)",
                       d3d8_TexReplEnabled() ? "enabled" : "disabled (set XBOX_TEXTURES)",
                       g_texrepl_hits, g_texrepl_misses);
            } else {
                REPORT(5, 0, "%s", "LockRect gave no pixels");
            }
            tex->lpVtbl->Release(tex);
        }
    }

summary:
    fprintf(stderr, "\n[HARNESS] ==================== summary ====================\n");
    {
        static const char *names[6] = {
            "", "create device", "clear+present", "d3d8 triangle",
            "nv2a pushbuffer", "texture replace"
        };
        for (int s = 1; s <= 5; s++)
            fprintf(stderr, "[HARNESS]   stage %d  %-16s %s\n",
                    s, names[s], g_stage_ok[s] ? "PASS" : "FAIL");
    }
    fprintf(stderr, "[HARNESS] ================================================\n");
    fflush(stderr);

    return (g_stage_ok[1] && g_stage_ok[2]) ? 0 : 1;
}
