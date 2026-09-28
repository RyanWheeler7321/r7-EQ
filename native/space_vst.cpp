// R7 Space: stereo width with centred bass, speaker "3D" side shaping, and a small room.
// Plain VST2 ABI (no SDK). Mid (mono/centre, e.g. voices) is never filtered by Width or 3D.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <xmmintrin.h>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>

struct Effect;
using Callback = intptr_t (__cdecl *)(Effect*, int32_t, int32_t, intptr_t, void*, float);
using SetParameter = void (__cdecl *)(Effect*, int32_t, float);
using GetParameter = float (__cdecl *)(Effect*, int32_t);
using Process = void (__cdecl *)(Effect*, float**, float**, int32_t);
struct Effect {
    int32_t magic; Callback dispatcher; Process process; SetParameter setParameter;
    GetParameter getParameter; int32_t numPrograms, numParams, numInputs, numOutputs, flags;
    intptr_t reserved1, reserved2; int32_t initialDelay, realQualities, offQualities;
    float ioRatio; void* object; void* user; int32_t uniqueID, version;
    Process processReplacing; void* processDoubleReplacing; char future[56];
};

enum Param { Width, Depth, Room, Size, ParamCount };
static const char* const ParamNames[ParamCount] = {"Width", "3D", "Room", "Size"};
static const float ParamDefaults[ParamCount] = {0.5f, 0.f, 0.f, 0.5f};

static constexpr int RingSize = 16384;   // > 85 ms at 192 kHz: longest room delay is ~59 ms.
static constexpr int RingMask = RingSize - 1;
static constexpr int Lines = 4;
static constexpr float Pi = 3.14159265358979f;

// Early reflections (ms at Size 50%) and alternating-sign gains, decorrelated left/right.
static const float ErLeftMs[6] = {4.3f, 7.9f, 11.6f, 17.3f, 23.1f, 31.9f};
static const float ErRightMs[6] = {5.2f, 9.1f, 13.7f, 19.4f, 26.8f, 35.3f};
static const float ErGain[6] = {0.62f, -0.51f, 0.44f, -0.37f, 0.30f, -0.24f};
static const float LineMs[Lines] = {23.3f, 29.1f, 35.9f, 41.7f};

struct Biquad {
    float b0 = 1, b1 = 0, b2 = 0, a1 = 0, a2 = 0, z1 = 0, z2 = 0;
    void lowpass(float hz, float rate) {
        float w = 2 * Pi * hz / rate, cw = std::cos(w), alpha = std::sin(w) / (2 * 0.70710678f);
        float a0 = 1 + alpha;
        b0 = (1 - cw) / 2 / a0; b1 = (1 - cw) / a0; b2 = b0; a1 = -2 * cw / a0; a2 = (1 - alpha) / a0;
    }
    void highpass(float hz, float rate) {
        float w = 2 * Pi * hz / rate, cw = std::cos(w), alpha = std::sin(w) / (2 * 0.70710678f);
        float a0 = 1 + alpha;
        b0 = (1 + cw) / 2 / a0; b1 = -(1 + cw) / a0; b2 = b0; a1 = -2 * cw / a0; a2 = (1 - alpha) / a0;
    }
    void clear() { z1 = z2 = 0; }
    float run(float x) {
        float y = b0 * x + z1;
        z1 = b1 * x - a1 * y + z2; z2 = b2 * x - a2 * y;
        return y;
    }
};
struct OnePole {  // y += k (x - y): lowpass; x - lowpass(x): highpass.
    float k = 1, y = 0;
    void set(float hz, float rate) { k = 1 - std::exp(-2 * Pi * hz / rate); }
    float low(float x) { y += k * (x - y); return y; }
    float high(float x) { return x - low(x); }
};

struct State {
    Effect effect;
    float params[ParamCount];
    volatile LONG dirty;
    float rate;
    // Derived per-sample values (audio thread only).
    float lowWidth, highWidth, depthGain, wet, feedback[Lines];
    int shapeDelay, erLeft[6], erRight[6], lineDelay[Lines];
    bool roomActive, split;
    // Linkwitz-Riley 4th order: bands sum to an allpass. Mid runs through the same allpass so
    // mid/side still recombine into the original left/right placement.
    Biquad sideLow[2], sideHigh[2], midLow[2], midHigh[2];
    OnePole shapeHigh, shapeLow, roomHigh, roomDamp, lineDamp[Lines];
    float* sideRing; float* roomRing; float* lineRing[Lines];
    int position;
};
static State* state(Effect* e) { return static_cast<State*>(e->object); }

static void clearRoom(State* s) {
    std::memset(s->roomRing, 0, sizeof(float) * RingSize);
    for (int i = 0; i < Lines; ++i) { std::memset(s->lineRing[i], 0, sizeof(float) * RingSize); s->lineDamp[i].y = 0; }
    s->roomHigh.y = 0; s->roomDamp.y = 0;
}
static void clearSplit(State* s) {
    for (int i = 0; i < 2; ++i) { s->sideLow[i].clear(); s->sideHigh[i].clear(); s->midLow[i].clear(); s->midHigh[i].clear(); }
    std::memset(s->sideRing, 0, sizeof(float) * RingSize);
    s->shapeHigh.y = s->shapeLow.y = 0;
}
static void clearAll(State* s) {
    clearSplit(s);
    clearRoom(s);
}
static int samples(float ms, float rate) {
    int n = static_cast<int>(ms * 0.001f * rate + 0.5f);
    return n < 1 ? 1 : (n > RingSize - 1 ? RingSize - 1 : n);
}
static void configure(State* s) {
    float rate = s->rate;
    float width = 2 * s->params[Width];                 // 0..200 %
    s->highWidth = width;
    s->lowWidth = width < 1 ? width : 1;                 // Bass below ~150 Hz never widens.
    s->depthGain = 0.8f * s->params[Depth];
    s->wet = 0.5f * s->params[Room];
    for (int i = 0; i < 2; ++i) {
        s->sideLow[i].lowpass(150, rate); s->sideHigh[i].highpass(150, rate);
        s->midLow[i].lowpass(150, rate); s->midHigh[i].highpass(150, rate);
    }
    // Narrowing (or neutral) scales the whole side exactly; only widening/3D need the crossover.
    bool split = s->highWidth != s->lowWidth || s->depthGain > 0;
    if (split && !s->split) clearSplit(s);
    s->split = split;
    s->shapeHigh.set(300, rate); s->shapeLow.set(4000, rate);   // Head-shadow band for 3D.
    s->shapeDelay = samples(0.22f, rate);                        // Ear-to-ear path difference.
    float scale = 0.6f + 0.8f * s->params[Size];
    float rt60 = 0.2f + 0.8f * s->params[Size];
    for (int i = 0; i < 6; ++i) { s->erLeft[i] = samples(ErLeftMs[i] * scale, rate); s->erRight[i] = samples(ErRightMs[i] * scale, rate); }
    for (int i = 0; i < Lines; ++i) {
        s->lineDelay[i] = samples(LineMs[i] * scale, rate);
        s->feedback[i] = std::pow(10.f, -3.f * (s->lineDelay[i] / rate) / rt60);
        s->lineDamp[i].set(5500 - 2500 * s->params[Size], rate);
    }
    s->roomHigh.set(200, rate); s->roomDamp.set(7000, rate);
    bool active = s->wet > 0;
    if (active && !s->roomActive) clearRoom(s);          // No stale tail when Room returns.
    s->roomActive = active;
}

static void __cdecl process(Effect* e, float** input, float** output, int32_t frames) {
    State* s = state(e);
    unsigned int csr = _mm_getcsr();
    _mm_setcsr(csr | 0x8040);                            // Flush denormals in decaying tails.
    if (InterlockedExchange(&s->dirty, 0)) configure(s);
    float* inL = input[0]; float* inR = input[1];
    float* outL = output[0]; float* outR = output[1];
    int p = s->position;
    for (int i = 0; i < frames; ++i) {
        float left = inL[i], right = inR[i];           // Read first: host buffers may alias.
        float mid = 0.5f * (left + right), side = 0.5f * (left - right);
        float newSide = s->highWidth * side;
        if (s->split) {
            mid = s->midLow[1].run(s->midLow[0].run(mid)) + s->midHigh[1].run(s->midHigh[0].run(mid));
            float low = s->sideLow[1].run(s->sideLow[0].run(side));
            float high = s->sideHigh[1].run(s->sideHigh[0].run(side));
            // Partial crosstalk cancellation on the side signal: each speaker also plays the other
            // channel's side, inverted, delayed by the ear-to-ear path and band-limited like head shadow.
            s->sideRing[p] = high;
            float crossed = s->sideRing[(p - s->shapeDelay) & RingMask];
            float shaped = high + s->depthGain * s->shapeLow.low(s->shapeHigh.high(crossed));
            newSide = s->lowWidth * low + s->highWidth * shaped;
        }
        float l = mid + newSide, r = mid - newSide;
        if (s->roomActive) {
            s->roomRing[p] = s->roomDamp.low(s->roomHigh.high(mid));
            float erL = 0, erR = 0;
            for (int t = 0; t < 6; ++t) {
                erL += ErGain[t] * s->roomRing[(p - s->erLeft[t]) & RingMask];
                erR += ErGain[t] * s->roomRing[(p - s->erRight[t]) & RingMask];
            }
            float y[Lines];
            for (int n = 0; n < Lines; ++n) y[n] = s->lineRing[n][(p - s->lineDelay[n]) & RingMask];
            // 4x4 Hadamard feedback (energy preserving), damped per line.
            float h0 = 0.5f * (y[0] + y[1] + y[2] + y[3]), h1 = 0.5f * (y[0] - y[1] + y[2] - y[3]);
            float h2 = 0.5f * (y[0] + y[1] - y[2] - y[3]), h3 = 0.5f * (y[0] - y[1] - y[2] + y[3]);
            float feed = 0.35f * (erL + erR);
            float h[Lines] = {h0, h1, h2, h3};
            for (int n = 0; n < Lines; ++n)
                s->lineRing[n][p] = feed + s->feedback[n] * s->lineDamp[n].low(h[n]);
            l += s->wet * (0.8f * erL + 0.7f * (y[0] + y[2]) * 0.5f);
            r += s->wet * (0.8f * erR + 0.7f * (y[1] + y[3]) * 0.5f);
        }
        outL[i] = l; outR[i] = r;
        p = (p + 1) & RingMask;
    }
    s->position = p;
    _mm_setcsr(csr);
}

static void __cdecl set(Effect* e, int32_t index, float value) {
    if (index < 0 || index >= ParamCount) return;
    State* s = state(e);
    s->params[index] = value < 0 ? 0 : (value > 1 ? 1 : value);
    InterlockedExchange(&s->dirty, 1);
}
static float __cdecl get(Effect* e, int32_t index) {
    return index >= 0 && index < ParamCount ? state(e)->params[index] : 0.f;
}
static void copyText(void* ptr, const char* text, size_t size = 8) {
    if (ptr) strncpy_s(static_cast<char*>(ptr), size + 1, text, _TRUNCATE);
}
static intptr_t __cdecl dispatch(Effect* e, int32_t op, int32_t index, intptr_t value, void* ptr, float opt) {
    State* s = state(e);
    switch (op) {
    case 1:  // effClose
        HeapFree(GetProcessHeap(), 0, s->sideRing);
        HeapFree(GetProcessHeap(), 0, s);
        return 1;
    case 6: copyText(ptr, "%"); return 1;
    case 7: {
        if (index < 0 || index >= ParamCount) return 0;
        char text[16];
        float scale = index == Width ? 200.f : 100.f;
        std::snprintf(text, sizeof(text), "%.0f", s->params[index] * scale);
        copyText(ptr, text);
        return 1;
    }
    case 8: if (index < 0 || index >= ParamCount) return 0; copyText(ptr, ParamNames[index]); return 1;
    case 10: if (opt > 1000 && opt <= 384000) { s->rate = opt; InterlockedExchange(&s->dirty, 1); } return 1;
    case 12: if (value) { clearAll(s); InterlockedExchange(&s->dirty, 1); } return 1;  // resume
    case 45: copyText(ptr, "R7 Space", 32); return 1;
    case 47: copyText(ptr, "Ryan", 64); return 1;
    case 48: copyText(ptr, "R7 Space", 64); return 1;
    case 49: return 1;
    case 58: return 2400;
    default: return 0;
    }
}

extern "C" __declspec(dllexport) Effect* __cdecl VSTPluginMain(Callback) {
    HANDLE heap = GetProcessHeap();
    State* s = static_cast<State*>(HeapAlloc(heap, HEAP_ZERO_MEMORY, sizeof(State)));
    if (!s) return nullptr;
    float* rings = static_cast<float*>(HeapAlloc(heap, HEAP_ZERO_MEMORY, sizeof(float) * RingSize * (2 + Lines)));
    if (!rings) { HeapFree(heap, 0, s); return nullptr; }
    s->sideRing = rings; s->roomRing = rings + RingSize;
    for (int i = 0; i < Lines; ++i) s->lineRing[i] = rings + RingSize * (2 + i);
    for (int i = 0; i < ParamCount; ++i) s->params[i] = ParamDefaults[i];
    s->rate = 48000; s->dirty = 1;
    Effect& f = s->effect;
    f.magic = 0x56737450; f.dispatcher = dispatch; f.process = process; f.setParameter = set;
    f.getParameter = get; f.numPrograms = 0; f.numParams = ParamCount; f.numInputs = 2; f.numOutputs = 2;
    f.flags = 1 << 4;  // effFlagsCanReplacing
    f.ioRatio = 1; f.object = s; f.uniqueID = 0x52375350; f.version = 1;
    f.processReplacing = process;
    return &f;
}
BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(instance);
    return TRUE;
}
