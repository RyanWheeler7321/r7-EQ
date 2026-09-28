// r7 Compressor: Chromium's Web Audio DynamicsCompressor with r7's own makeup gain in place of
// Chrome's automatic one, a dry blend, a detector that can ignore bass, and the live boost meter
// r7-EQ reads.
//
// The compressor itself follows third_party/blink/renderer/platform/audio/dynamics_compressor.cc:
//
// Copyright (C) 2011 Google Inc. All rights reserved.
//
// Redistribution and use in source and binary forms, with or without
// modification, are permitted provided that the following conditions
// are met:
//
// 1.  Redistributions of source code must retain the above copyright
//     notice, this list of conditions and the following disclaimer.
// 2.  Redistributions in binary form must reproduce the above copyright
//     notice, this list of conditions and the following disclaimer in the
//     documentation and/or other materials provided with the distribution.
// 3.  Neither the name of Apple Computer, Inc. ("Apple") nor the names of
//     its contributors may be used to endorse or promote products derived
//     from this software without specific prior written permission.
//
// THIS SOFTWARE IS PROVIDED BY APPLE AND ITS CONTRIBUTORS "AS IS" AND ANY
// EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
// WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
// DISCLAIMED. IN NO EVENT SHALL APPLE OR ITS CONTRIBUTORS BE LIABLE FOR ANY
// DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
// (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
// LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
// ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
// (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
// THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
//
// Parameters (0..1 each): Thresh -100..0 dB, Knee 0..40 dB, Ratio 1..100, Attack 0..1 s,
// Release 0..1 s, Makeup 0..48 dB, Dry 0..1 (linear), Bass 0..1000 Hz (detector high-pass,
// 0 = off), Slot (slot / 1000) and Meter (on at >= 0.5).
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <sddl.h>
#include <xmmintrin.h>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cwchar>

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
struct Meter {
    uint32_t magic, version; volatile LONG sequence; uint32_t pid;
    uint64_t instance, tick; float boost, maximum, inputPeak, outputPeak;
    uint32_t signal, reserved;
};
static_assert(sizeof(Meter) == 56, "Meter protocol changed");

enum { Thresh, Knee, Ratio, Attack, Release, Makeup, Dry, Bass, Slot, MeterOn, ParamCount };
static const char* const Names[ParamCount] = {"Thresh", "Knee", "Ratio", "Attack", "Release",
                                              "Makeup", "Dry", "Bass", "Slot", "Meter"};
static constexpr int MaxDelay = 2048;              // 6 ms needs 1152 samples at 192 kHz.
static constexpr float PreDelay = 0.006f;          // Chrome's lookahead.
static constexpr float SatReleaseTime = 0.0025f;   // Chrome's detector release.
static constexpr float HalfPi = 1.57079632679489661923f;
// Chrome's adaptive release curve: release time scales with how far the gain has to recover.
static constexpr float Zone1 = 0.09f, Zone2 = 0.16f, Zone3 = 0.42f, Zone4 = 0.98f;
static constexpr float ABase = 0.9999999999999998f * Zone1 + 1.8432219684323923e-16f * Zone2 -
                               1.9373394351676423e-16f * Zone3 + 8.824516011816245e-18f * Zone4;
static constexpr float BBase = -1.5788320352845888f * Zone1 + 2.3305837032074286f * Zone2 -
                               0.9141194204840429f * Zone3 + 0.1623677525612032f * Zone4;
static constexpr float CBase = 0.5334142869106424f * Zone1 - 1.272736789213631f * Zone2 +
                               0.9258856042207512f * Zone3 - 0.18656310191776226f * Zone4;
static constexpr float DBase = 0.08783463138207234f * Zone1 - 0.1694162967925622f * Zone2 +
                               0.08588057951595272f * Zone3 - 0.00429891410546283f * Zone4;
static constexpr float EBase = -0.042416883008123074f * Zone1 + 0.1115693827987602f * Zone2 -
                               0.09764676325265872f * Zone3 + 0.028494263462021576f * Zone4;

struct State {
    Effect effect;
    float params[ParamCount];
    volatile LONG dirty;
    float rate;
    // Settings in real units, refreshed from params between blocks.
    float threshold, knee, ratio, attack, release, makeup, dry, bassHz;
    // Static curve (Chrome's UpdateStaticCurveParameters).
    float curveThreshold, curveKnee, curveRatio;
    float linearThreshold, slope, kneeThreshold, dbKneeThreshold, dbYKneeThreshold, k;
    // Envelope.
    float detectorAverage, compressorGain, maxAttackDiff;
    int delayFrames, readIndex, writeIndex;
    float delayL[MaxDelay], delayR[MaxDelay];
    // Detector high-pass (second-order Butterworth), per channel.
    bool highpass;
    float b0, b1, b2, a1, a2, zl[2], zr[2];
    // Meter.
    HANDLE mapping, request; Meter* meter; uint64_t instance; bool telemetry; int slot;
};
static State* state(Effect* e) { return static_cast<State*>(e->object); }

static float toDb(float linear) { return 20.f * std::log10(linear); }
static float toLinear(float db) { return std::pow(10.f, 0.05f * db); }
static float finite(float x, float fallback) { return std::isfinite(x) ? x : fallback; }

// Chrome's knee: linear up to the threshold, then an exponential that approaches
// linearThreshold + 1/k; its slope matches the ratio where the knee ends.
static float kneeCurve(const State* s, float x, float k) {
    if (x < s->linearThreshold) return x;
    return s->linearThreshold + (1.f - static_cast<float>(std::exp(-static_cast<double>(k) * (x - s->linearThreshold)))) / k;
}
static float saturate(const State* s, float x, float k) {
    if (x < s->kneeThreshold) return kneeCurve(s, x, k);
    return toLinear(s->dbYKneeThreshold + s->slope * (toDb(x) - s->dbKneeThreshold));
}
static float kAtSlope(const State* s, float desiredSlope) {
    float dbX = s->curveThreshold + s->curveKnee;
    float x = toLinear(dbX), x2 = 1, dbX2 = 0;
    if (!(x < s->linearThreshold)) { x2 = x * 1.001f; dbX2 = toDb(x2); }
    float minK = 0.1f, maxK = 10000, k = 5, slope = 1;
    for (int i = 0; i < 15; ++i) {
        if (!(x < s->linearThreshold)) slope = (toDb(kneeCurve(s, x2, k)) - toDb(kneeCurve(s, x, k))) / (dbX2 - dbX);
        if (slope < desiredSlope) maxK = k; else minK = k;
        k = std::sqrt(minK * maxK);
    }
    return k;
}
static float updateCurve(State* s) {
    if (s->threshold != s->curveThreshold || s->knee != s->curveKnee || s->ratio != s->curveRatio) {
        s->curveThreshold = s->threshold; s->curveKnee = s->knee; s->curveRatio = s->ratio;
        s->linearThreshold = toLinear(s->threshold);
        s->slope = 1.f / s->ratio;
        float k = kAtSlope(s, s->slope);
        s->dbKneeThreshold = s->threshold + s->knee;
        s->kneeThreshold = toLinear(s->dbKneeThreshold);
        s->dbYKneeThreshold = toDb(kneeCurve(s, s->kneeThreshold, k));
        s->k = k;
    }
    return s->k;
}

static void reset(State* s) {
    s->detectorAverage = 0; s->compressorGain = 1; s->maxAttackDiff = -1;
    std::memset(s->delayL, 0, sizeof(s->delayL)); std::memset(s->delayR, 0, sizeof(s->delayR));
    s->readIndex = 0; s->writeIndex = s->delayFrames;
    s->zl[0] = s->zl[1] = s->zr[0] = s->zr[1] = 0;
}
static void configure(State* s) {
    const float* p = s->params;
    s->threshold = -100.f + 100.f * p[Thresh];
    s->knee = 40.f * p[Knee];
    s->ratio = 1.f + 99.f * p[Ratio];
    s->attack = p[Attack];
    s->release = p[Release] < 0.0005f ? 0.0005f : p[Release];
    s->makeup = toLinear(48.f * p[Makeup]);
    s->dry = p[Dry];
    float bass = 1000.f * p[Bass];
    s->highpass = bass >= 1.f;
    if (s->highpass) {
        // RBJ cookbook high-pass, Q = 1/sqrt(2).
        float w = 2.f * 3.14159265358979f * bass / s->rate, c = std::cos(w), alpha = std::sin(w) / std::sqrt(2.f);
        float a0 = 1.f + alpha;
        s->b0 = (1.f + c) / 2.f / a0; s->b1 = -(1.f + c) / a0; s->b2 = s->b0;
        s->a1 = -2.f * c / a0; s->a2 = (1.f - alpha) / a0;
    }
    s->bassHz = bass;
    int frames = static_cast<int>(PreDelay * s->rate);
    if (frames > MaxDelay - 1) frames = MaxDelay - 1;
    if (frames != s->delayFrames) { s->delayFrames = frames; s->effect.initialDelay = frames; reset(s); }
}

static void closeMeter(State* s) {
    if (s->meter) UnmapViewOfFile(s->meter);
    if (s->mapping) CloseHandle(s->mapping);
    if (s->request) CloseHandle(s->request);
    s->meter = nullptr; s->mapping = nullptr; s->request = nullptr;
}
static void openMeter(State* s) {
    if (s->mapping || !s->telemetry || s->slot < 1) return;
    wchar_t mappingName[64], requestName[64];
    swprintf_s(mappingName, L"Global\\R7EQ.Dynboost.%d.v1", s->slot);
    swprintf_s(requestName, L"Global\\R7EQ.Dynboost.Request.%d.v1", s->slot);
    PSECURITY_DESCRIPTOR descriptor = nullptr;
    // Audio service/SYSTEM write; signed-in users may only read these scalar meters.
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(
            L"D:P(A;;GA;;;SY)(A;;GA;;;LS)(A;;GR;;;AU)", SDDL_REVISION_1, &descriptor, nullptr)) return;
    SECURITY_ATTRIBUTES sa{sizeof(sa), descriptor, FALSE};
    s->mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, &sa, PAGE_READWRITE, 0, sizeof(Meter), mappingName);
    bool fresh = GetLastError() != ERROR_ALREADY_EXISTS;
    LocalFree(descriptor);
    if (!s->mapping) return;
    s->meter = static_cast<Meter*>(MapViewOfFile(s->mapping, FILE_MAP_WRITE, 0, 0, sizeof(Meter)));
    if (!s->meter) { CloseHandle(s->mapping); s->mapping = nullptr; return; }
    if (fresh) {
        std::memset(s->meter, 0, sizeof(Meter));
        s->meter->magic = 0x52374d31; s->meter->version = 1;
    }
    descriptor = nullptr;
    // Auto-reset requests authorize one measured audio block, never continuous work.
    if (ConvertStringSecurityDescriptorToSecurityDescriptorW(
            L"D:P(A;;GA;;;SY)(A;;GA;;;LS)(A;;0x00000002;;;AU)", SDDL_REVISION_1, &descriptor, nullptr)) {
        sa.lpSecurityDescriptor = descriptor;
        s->request = CreateEventW(&sa, FALSE, FALSE, requestName);
        LocalFree(descriptor);
    }
}
static void publish(State* s, double inputEnergy, double outputEnergy, float inputPeak, float outputPeak) {
    Meter* m = s->meter;
    if (!m || m->magic != 0x52374d31 || m->version != 1) return;
    LONG seq = m->sequence;
    if ((seq & 1) || InterlockedCompareExchange(&m->sequence, seq + 1, seq) != seq) return;
    if (s->instance >= m->instance) {
        m->pid = GetCurrentProcessId(); m->instance = s->instance; m->tick = GetTickCount64();
        m->signal = inputEnergy > 1e-24 ? 1 : 0;
        m->boost = m->signal && outputEnergy > 0 ? static_cast<float>(10 * std::log10(outputEnergy / inputEnergy)) : 0;
        m->maximum = 48.f * s->params[Makeup];
        m->inputPeak = inputPeak; m->outputPeak = outputPeak; m->reserved = 0;
    }
    MemoryBarrier(); InterlockedExchange(&m->sequence, seq + 2);
}

static void __cdecl process(Effect* e, float** input, float** output, int32_t frames) {
    State* s = state(e);
    unsigned int csr = _mm_getcsr();
    _mm_setcsr(csr | 0x8040);
    if (InterlockedExchange(&s->dirty, 0)) configure(s);
    bool measure = s->telemetry && s->meter && s->request && WaitForSingleObject(s->request, 0) == WAIT_OBJECT_0;
    double inputEnergy = 0, outputEnergy = 0; float inputPeak = 0, outputPeak = 0;

    const float k = updateCurve(s);
    const float attackFrames = (s->attack > 0.001f ? s->attack : 0.001f) * s->rate;
    const float releaseFrames = s->rate * s->release;
    const float satReleaseFrames = SatReleaseTime * s->rate;
    const float a = releaseFrames * ABase, b = releaseFrames * BBase, c = releaseFrames * CBase;
    const float d = releaseFrames * DBase, e4 = releaseFrames * EBase;
    float* inL = input[0]; float* inR = input[1];
    float* outL = output[0]; float* outR = output[1];

    int frame = 0;
    while (frame < frames) {
        // Once per 32 frames: how fast to move toward the gain the detector asks for.
        s->detectorAverage = finite(s->detectorAverage, 1);
        const float scaledDesired = std::asin(s->detectorAverage) / HalfPi;
        const bool releasing = scaledDesired > s->compressorGain;
        float diff = scaledDesired == 0 ? (releasing ? -1.f : 1.f) : toDb(s->compressorGain / scaledDesired);
        float envelopeRate;
        if (releasing) {
            s->maxAttackDiff = -1;
            diff = finite(diff, -1);
            float x = diff < -12.f ? -12.f : (diff > 0.f ? 0.f : diff);
            x = 0.25f * (x + 12.f);
            const float x2 = x * x, x3 = x2 * x, x4 = x2 * x2;
            const float releaseFramesNow = a + b * x + c * x2 + d * x3 + e4 * x4;
            envelopeRate = toLinear(5.f / releaseFramesNow);
        } else {
            diff = finite(diff, 1);
            if (s->maxAttackDiff == -1 || s->maxAttackDiff < diff) s->maxAttackDiff = diff;
            const float attenuationDiff = s->maxAttackDiff > 0.5f ? s->maxAttackDiff : 0.5f;
            envelopeRate = 1.f - std::pow(0.25f / attenuationDiff, 1.f / attackFrames);
        }

        const int count = frames - frame < 32 ? frames - frame : 32;
        for (int i = 0; i < count; ++i, ++frame) {
            const float left = inL[frame], right = inR[frame];   // Read first: buffers may alias.
            s->delayL[s->writeIndex] = left; s->delayR[s->writeIndex] = right;
            float heardL = left, heardR = right;
            if (s->highpass) {
                heardL = s->b0 * left + s->zl[0]; s->zl[0] = s->b1 * left - s->a1 * heardL + s->zl[1]; s->zl[1] = s->b2 * left - s->a2 * heardL;
                heardR = s->b0 * right + s->zr[0]; s->zr[0] = s->b1 * right - s->a1 * heardR + s->zr[1]; s->zr[1] = s->b2 * right - s->a2 * heardR;
            }
            const float level = std::fabs(heardL) > std::fabs(heardR) ? std::fabs(heardL) : std::fabs(heardR);

            // Detector: attenuation the curve wants for this sample, released at Chrome's fixed rate.
            const float attenuation = level <= 0.0001f ? 1.f : saturate(s, level, k) / level;
            float dbAttenuation = -toDb(attenuation);
            if (dbAttenuation < 2.f) dbAttenuation = 2.f;
            const float satReleaseRate = toLinear(dbAttenuation / satReleaseFrames) - 1.f;
            const float rate = attenuation > s->detectorAverage ? satReleaseRate : 1.f;
            s->detectorAverage += (attenuation - s->detectorAverage) * rate;
            if (s->detectorAverage > 1.f) s->detectorAverage = 1.f;
            s->detectorAverage = finite(s->detectorAverage, 1);

            if (envelopeRate < 1) s->compressorGain += (scaledDesired - s->compressorGain) * envelopeRate;
            else { s->compressorGain *= envelopeRate; if (s->compressorGain > 1.f) s->compressorGain = 1.f; }
            const float gain = s->makeup * static_cast<float>(std::sin(static_cast<double>(HalfPi * s->compressorGain)));

            const float delayedL = s->delayL[s->readIndex], delayedR = s->delayR[s->readIndex];
            const float yl = delayedL * (gain + s->dry), yr = delayedR * (gain + s->dry);
            outL[frame] = yl; outR[frame] = yr;
            if (measure) {
                inputEnergy += double(left) * left + double(right) * right;
                outputEnergy += double(yl) * yl + double(yr) * yr;
                const float inPeak = std::fabs(left) > std::fabs(right) ? std::fabs(left) : std::fabs(right);
                const float outPeak = std::fabs(yl) > std::fabs(yr) ? std::fabs(yl) : std::fabs(yr);
                if (inPeak > inputPeak) inputPeak = inPeak;
                if (outPeak > outputPeak) outputPeak = outPeak;
            }
            s->readIndex = (s->readIndex + 1) % MaxDelay;
            s->writeIndex = (s->writeIndex + 1) % MaxDelay;
        }
    }
    if (std::fabs(s->detectorAverage) < 1e-30f) s->detectorAverage = 0;
    if (std::fabs(s->compressorGain) < 1e-30f) s->compressorGain = 0;
    if (measure) publish(s, inputEnergy, outputEnergy, inputPeak, outputPeak);
    _mm_setcsr(csr);
}

static void __cdecl set(Effect* e, int32_t index, float value) {
    State* s = state(e);
    if (index < 0 || index >= ParamCount) return;
    value = value < 0 ? 0 : (value > 1 ? 1 : value);
    s->params[index] = value;
    // The host sets both once while loading, before audio runs; a meter never changes slot.
    if (index == Slot && !s->mapping) { s->slot = static_cast<int>(std::lround(value * 1000)); openMeter(s); }
    else if (index == MeterOn) { s->telemetry = value >= .5f; openMeter(s); }
    else InterlockedExchange(&s->dirty, 1);
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
    case 1: closeMeter(s); HeapFree(GetProcessHeap(), 0, s); return 1;
    case 6: case 7: case 8: {
        if (index < 0 || index >= ParamCount) return 0;
        if (op == 8) { copyText(ptr, Names[index]); return 1; }
        static const char* const Labels[ParamCount] = {"dB", "dB", ":1", "ms", "ms", "dB", "%", "Hz", "", ""};
        if (op == 6) { copyText(ptr, Labels[index]); return 1; }
        const float p = s->params[index];
        const float shown[ParamCount] = {-100.f + 100.f * p, 40.f * p, 1.f + 99.f * p, 1000.f * p, 1000.f * p,
                                         48.f * p, 100.f * p, 1000.f * p, static_cast<float>(s->slot), p >= .5f ? 1.f : 0.f};
        char text[16];
        std::snprintf(text, sizeof(text), index >= Slot ? "%.0f" : "%.1f", shown[index]);
        copyText(ptr, text);
        return 1;
    }
    case 10: if (opt > 1000 && opt <= 384000) { s->rate = opt; InterlockedExchange(&s->dirty, 1); } return 1;
    case 12: if (value) { configure(s); reset(s); } return 1;
    case 45: copyText(ptr, "r7 Compressor", 32); return 1;
    case 47: copyText(ptr, "Ryan", 64); return 1;
    case 48: copyText(ptr, "r7 Compressor", 64); return 1;
    case 49: return 1;
    case 58: return 2400;
    default: return 0;
    }
}

extern "C" __declspec(dllexport) Effect* __cdecl VSTPluginMain(Callback) {
    State* s = static_cast<State*>(HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(State)));
    if (!s) return nullptr;
    // Twitch's defaults, no makeup: -50 dB, 40 dB knee, 12:1, 0 ms attack, 250 ms release.
    s->params[Thresh] = 0.5f; s->params[Knee] = 1.f; s->params[Ratio] = 11.f / 99.f; s->params[Release] = 0.25f;
    s->rate = 48000; s->delayFrames = -1;
    s->curveThreshold = s->curveKnee = s->curveRatio = -1;
    configure(s);
    LARGE_INTEGER tick; QueryPerformanceCounter(&tick); s->instance = tick.QuadPart;
    Effect& f = s->effect;
    f.magic = 0x56737450; f.dispatcher = dispatch; f.process = process; f.setParameter = set;
    f.getParameter = get; f.numParams = ParamCount; f.numInputs = 2; f.numOutputs = 2;
    f.flags = 1 << 4; f.ioRatio = 1; f.object = s; f.uniqueID = 0x52374350; f.version = 1;
    f.processReplacing = process;
    f.initialDelay = s->delayFrames;
    return &f;
}
BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(instance);
    return TRUE;
}
