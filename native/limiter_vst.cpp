// r7 Limiter: stereo-linked lookahead peak limiter, the last stage of the r7-EQ chain.
// Guarantee: |output| <= ceiling. The gain is the minimum required gain held over the lookahead
// window, then box-smoothed over the same window (so it has fully reached the required gain when
// the peak leaves the delay line), then released slowly. Quiet material passes untouched.
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

static constexpr int MaxLookahead = 1024;         // 2 ms needs 384 samples even at 192 kHz.
static constexpr float LookaheadMs = 2.0f;
static constexpr float ReleaseMs = 150.0f;

struct State {
    Effect effect;
    float ceilingParam;                   // 0..1 -> -3..0 dBFS
    volatile LONG dirty;
    float rate, ceiling, release;
    int lookahead, position;
    float delayL[MaxLookahead], delayR[MaxLookahead];
    float required[MaxLookahead];         // Required gain per recent input sample.
    float held[MaxLookahead];             // Held minimum per recent sample (box filter input).
    double heldSum;
    float gain;
    int sinceResum;
};
static State* state(Effect* e) { return static_cast<State*>(e->object); }

static void reset(State* s) {
    std::memset(s->delayL, 0, sizeof(s->delayL)); std::memset(s->delayR, 0, sizeof(s->delayR));
    for (int i = 0; i < MaxLookahead; ++i) { s->required[i] = 1.f; s->held[i] = 1.f; }
    s->heldSum = s->lookahead; s->gain = 1.f; s->position = 0; s->sinceResum = 0;
}
static void configure(State* s) {
    int lookahead = static_cast<int>(LookaheadMs * 0.001f * s->rate + 0.5f);
    lookahead = lookahead < 1 ? 1 : (lookahead > MaxLookahead ? MaxLookahead : lookahead);
    bool resized = lookahead != s->lookahead;
    s->lookahead = lookahead;
    s->ceiling = std::pow(10.f, (-3.f + 3.f * s->ceilingParam) / 20.f);
    s->release = 1.f - std::exp(-1.f / (ReleaseMs * 0.001f * s->rate));
    s->effect.initialDelay = lookahead;
    if (resized) reset(s);
}

static void __cdecl process(Effect* e, float** input, float** output, int32_t frames) {
    State* s = state(e);
    unsigned int csr = _mm_getcsr();
    _mm_setcsr(csr | 0x8040);
    if (InterlockedExchange(&s->dirty, 0)) configure(s);
    const int n = s->lookahead;
    const float ceiling = s->ceiling;
    float* inL = input[0]; float* inR = input[1];
    float* outL = output[0]; float* outR = output[1];
    for (int i = 0; i < frames; ++i) {
        float left = inL[i], right = inR[i];                  // Read first: buffers may alias.
        int p = s->position;
        float peak = std::fabs(left) > std::fabs(right) ? std::fabs(left) : std::fabs(right);
        s->required[p] = peak > ceiling ? ceiling / peak : 1.f;
        // Hold: minimum required gain over the last n+1 input samples (this one included).
        float hold = s->required[p];
        for (int k = 1; k <= n; ++k) {
            float r = s->required[(p - k + MaxLookahead) % MaxLookahead];
            if (r < hold) hold = r;
        }
        // Box filter over the last n held values: reaches `hold` exactly n samples later.
        int oldest = (p - n + MaxLookahead) % MaxLookahead;
        s->heldSum += hold - s->held[oldest];
        s->held[p] = hold;
        if (++s->sinceResum >= 8192) {                          // Remove float drift.
            double sum = 0;
            for (int k = 0; k < n; ++k) sum += s->held[(p - k + MaxLookahead) % MaxLookahead];
            s->heldSum = sum; s->sinceResum = 0;
        }
        float target = static_cast<float>(s->heldSum / n);
        // Attack is already smoothed by the box; release recovers slowly and never overshoots it.
        s->gain = target < s->gain ? target : s->gain + (target - s->gain) * s->release;
        if (s->gain > target) s->gain = target;
        float dl = s->delayL[oldest], dr = s->delayR[oldest];   // Input from n samples ago.
        s->delayL[p] = left; s->delayR[p] = right;
        float yl = dl * s->gain, yr = dr * s->gain;
        // Numerical backstop only; the gain above already guarantees the ceiling.
        outL[i] = yl > ceiling ? ceiling : (yl < -ceiling ? -ceiling : yl);
        outR[i] = yr > ceiling ? ceiling : (yr < -ceiling ? -ceiling : yr);
        s->position = (p + 1) % MaxLookahead;
    }
    _mm_setcsr(csr);
}

static void __cdecl set(Effect* e, int32_t index, float value) {
    if (index != 0) return;
    State* s = state(e);
    s->ceilingParam = value < 0 ? 0 : (value > 1 ? 1 : value);
    InterlockedExchange(&s->dirty, 1);
}
static float __cdecl get(Effect* e, int32_t index) { return index == 0 ? state(e)->ceilingParam : 0.f; }
static void copyText(void* ptr, const char* text, size_t size = 8) {
    if (ptr) strncpy_s(static_cast<char*>(ptr), size + 1, text, _TRUNCATE);
}
static intptr_t __cdecl dispatch(Effect* e, int32_t op, int32_t index, intptr_t value, void* ptr, float opt) {
    State* s = state(e);
    switch (op) {
    case 1: HeapFree(GetProcessHeap(), 0, s); return 1;
    case 6: copyText(ptr, "dB"); return 1;
    case 7: {
        if (index != 0) return 0;
        char text[16];
        std::snprintf(text, sizeof(text), "%.1f", -3.f + 3.f * s->ceilingParam);
        copyText(ptr, text);
        return 1;
    }
    case 8: if (index != 0) return 0; copyText(ptr, "Ceiling"); return 1;
    case 10: if (opt > 1000 && opt <= 384000) { s->rate = opt; InterlockedExchange(&s->dirty, 1); } return 1;
    case 12: if (value) { reset(s); InterlockedExchange(&s->dirty, 1); } return 1;
    case 45: copyText(ptr, "r7 Limiter", 32); return 1;
    case 47: copyText(ptr, "Ryan", 64); return 1;
    case 48: copyText(ptr, "r7 Limiter", 64); return 1;
    case 49: return 1;
    case 58: return 2400;
    default: return 0;
    }
}

extern "C" __declspec(dllexport) Effect* __cdecl VSTPluginMain(Callback) {
    State* s = static_cast<State*>(HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(State)));
    if (!s) return nullptr;
    s->ceilingParam = 0.9f;               // -0.3 dBFS
    s->rate = 48000; s->lookahead = 0;
    configure(s);
    Effect& f = s->effect;
    f.magic = 0x56737450; f.dispatcher = dispatch; f.process = process; f.setParameter = set;
    f.getParameter = get; f.numParams = 1; f.numInputs = 2; f.numOutputs = 2;
    f.flags = 1 << 4; f.ioRatio = 1; f.object = s; f.uniqueID = 0x52374c4d; f.version = 1;
    f.processReplacing = process;
    f.initialDelay = s->lookahead;
    return &f;
}
BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(instance);
    return TRUE;
}
