# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
import subprocess
import urllib.request
import wave
from collections.abc import Mapping
from pathlib import Path

from poe2brain.models.filters import (
    GeneratedSoundClip,
    SoundPackManifest,
    SoundPackStyle,
    SoundSynthesisParams,
)

SAMPLE_RATE: int = 44100
WARM_NIGHT_DEFAULT_LOWPASS_HZ: float = 2100.0
_BUTTERWORTH_Q_CASCADE: tuple[float, float] = (0.54119610, 1.30656296)

_CALLOUT_TIER_ALIASES: dict[str, str] = {
    "s+": "s_plus",
    "s-plus": "s_plus",
    "divine": "s_plus",
    "divines": "s_plus",
    "mirror": "s_plus",
    "jackpot": "s_plus",
    "build": "build_upgrade",
    "upgrade": "build_upgrade",
    "pob": "build_upgrade",
    "waystone": "waystone_high",
    "waystones": "waystone_high",
    "t16": "waystone_high",
    "t15": "waystone_high",
    "chance": "chancing",
}

NOTE_SEMITONES: dict[str, int] = {
    "C": 0,
    "C#": 1,
    "DB": 1,
    "D": 2,
    "D#": 3,
    "EB": 3,
    "E": 4,
    "F": 5,
    "F#": 6,
    "GB": 6,
    "G": 7,
    "G#": 8,
    "AB": 8,
    "A": 9,
    "A#": 10,
    "BB": 10,
    "B": 11,
}

SCALE_INTERVALS: dict[str, tuple[int, ...]] = {
    "lydian": (0, 2, 4, 6, 7, 9, 11, 12, 14, 16),
    "major": (0, 2, 4, 5, 7, 9, 11, 12, 14, 16),
    "minor": (0, 2, 3, 5, 7, 8, 10, 12, 14, 15),
    "pentatonic": (0, 2, 4, 7, 9, 12, 14, 16, 19, 21),
    "phrygian": (0, 1, 3, 5, 7, 8, 10, 12, 13, 15),
    "dorian": (0, 2, 3, 5, 7, 9, 10, 12, 14, 15),
}

DEFAULT_TIER_CALLOUTS: dict[str, str] = {
    "s_plus": "Divine Tier!",
    "s": "High Value Drop!",
    "a": "Notable Drop",
    "b": "Good Currency",
    "build_upgrade": "Build Upgrade Base!",
    "waystone_high": "High Tier Waystone",
    "chancing": "Chance Base",
}

DEFAULT_TIER_CALLOUTS_ZH: dict[str, str] = {
    "s_plus": "神圣石，出大货！",
    "s": "高价值掉落！",
    "a": "优质通货",
    "b": "实用通货",
    "build_upgrade": "核心装备底材！",
    "waystone_high": "高阶引路石",
    "chancing": "机会石底材",
}


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)

SOUND_PACK_TIERS: tuple[str, ...] = (
    "s_plus",
    "s",
    "a",
    "b",
    "c",
    "build_upgrade",
    "waystone_high",
    "chancing",
)


def _midi_to_hz(midi_note: float) -> float:
    return 440.0 * (2.0 ** ((midi_note - 69.0) / 12.0))


def scale_frequencies(
    key: str,
    scale: str,
    degrees: list[int],
    octave: int = 5,
) -> list[float]:
    semi = NOTE_SEMITONES.get(key.strip().upper(), 2)
    intervals = SCALE_INTERVALS.get(scale, SCALE_INTERVALS["lydian"])
    base_midi = 12 * (octave + 1) + semi
    return [_midi_to_hz(base_midi + intervals[min(d, len(intervals) - 1)]) for d in degrees]


def _normalize_callout_map(raw: Mapping[str, str] | None) -> dict[str, str]:
    if not raw:
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        clean_k = k.strip().lower().replace(" ", "_")
        canon_k = _CALLOUT_TIER_ALIASES.get(
            clean_k, _CALLOUT_TIER_ALIASES.get(k.strip().lower(), clean_k)
        )
        if isinstance(v, str) and v.strip():
            out[canon_k] = v.strip()
    return out


def _effective_lowpass_hz(params: SoundSynthesisParams) -> float | None:
    if params.lowpass_hz is not None:
        return float(params.lowpass_hz)
    if params.warm_night_mode:
        return WARM_NIGHT_DEFAULT_LOWPASS_HZ
    return None


def _run_biquad_mono(
    samples: list[float],
    b0: float,
    b1: float,
    b2: float,
    a1: float,
    a2: float,
) -> list[float]:
    out = [0.0] * len(samples)
    x1 = 0.0
    x2 = 0.0
    y1 = 0.0
    y2 = 0.0
    for i, x0 in enumerate(samples):
        y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out[i] = y0
        x2 = x1
        x1 = x0
        y2 = y1
        y1 = y0
    return out


def apply_lowpass_filter(
    left: list[float],
    right: list[float],
    cutoff_hz: float,
    sample_rate: int = SAMPLE_RATE,
) -> tuple[list[float], list[float]]:
    """Apply a 4th-order Butterworth IIR low-pass cascade (-24 dB/oct, zero passband ripple)."""
    fc = max(250.0, min(float(cutoff_hz), sample_rate * 0.45))
    w0 = 2.0 * math.pi * fc / sample_rate
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    out_l = list(left)
    out_r = list(right)
    for q_val in _BUTTERWORTH_Q_CASCADE:
        alpha = sin_w0 / (2.0 * q_val)
        inv_a0 = 1.0 / (1.0 + alpha)
        b1 = (1.0 - cos_w0) * inv_a0
        b0 = 0.5 * b1
        b2 = b0
        a1 = (-2.0 * cos_w0) * inv_a0
        a2 = (1.0 - alpha) * inv_a0
        out_l = _run_biquad_mono(out_l, b0, b1, b2, a1, a2)
        out_r = _run_biquad_mono(out_r, b0, b1, b2, a1, a2)
    return out_l, out_r


def measure_high_frequency_energy_ratio(
    samples: list[float],
    cutoff_hz: float = 2500.0,
    sample_rate: int = SAMPLE_RATE,
) -> float:
    """Return fraction [0.0, 1.0] of signal energy strictly above `cutoff_hz` via 4th-order HPF."""
    if not samples:
        return 0.0
    total_energy = sum(s * s for s in samples)
    if total_energy <= 1e-12:
        return 0.0
    fc = max(250.0, min(float(cutoff_hz), sample_rate * 0.45))
    w0 = 2.0 * math.pi * fc / sample_rate
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    hp = list(samples)
    for q_val in _BUTTERWORTH_Q_CASCADE:
        alpha = sin_w0 / (2.0 * q_val)
        inv_a0 = 1.0 / (1.0 + alpha)
        b0 = 0.5 * (1.0 + cos_w0) * inv_a0
        b1 = -(1.0 + cos_w0) * inv_a0
        b2 = b0
        a1 = (-2.0 * cos_w0) * inv_a0
        a2 = (1.0 - alpha) * inv_a0
        hp = _run_biquad_mono(hp, b0, b1, b2, a1, a2)
    hf_energy = sum(s * s for s in hp)
    return round(min(1.0, max(0.0, hf_energy / total_energy)), 6)


def detect_rhythmic_pulses(
    samples: list[float],
    sample_rate: int = SAMPLE_RATE,
    min_separation_seconds: float = 0.045,
) -> list[float]:
    """Detect distinct rhythmic note-onset peak timestamps (in seconds) with prominence gating."""
    n = len(samples)
    if n == 0:
        return []
    win = max(1, int(0.016 * sample_rate))
    # Two-pass boxcar (= triangular smoothing) eliminates carrier-frequency ripple cleanly
    pass1 = [0.0] * n
    running = 0.0
    for i, s in enumerate(samples):
        running += abs(s)
        if i >= win:
            running -= abs(samples[i - win])
        pass1[i] = running / min(i + 1, win)
    env = [0.0] * n
    running = 0.0
    for i, s in enumerate(pass1):
        running += s
        if i >= win:
            running -= pass1[i - win]
        env[i] = running / min(i + 1, win)

    peak_env = max(env, default=0.0)
    if peak_env <= 1e-6:
        return []
    thresh = peak_env * 0.22
    min_prominence = peak_env * 0.08
    min_sep = max(1, int(min_separation_seconds * sample_rate))

    peaks: list[int] = []
    valley_since_last = 0.0
    for i in range(1, n - 1):
        valley_since_last = min(valley_since_last, env[i])
        if env[i] >= thresh and env[i] >= env[i - 1] and env[i] > env[i + 1]:
            if not peaks:
                peaks.append(i)
                valley_since_last = env[i]
            elif (i - peaks[-1]) >= min_sep and (env[i] - valley_since_last) >= min_prominence:
                peaks.append(i)
                valley_since_last = env[i]
            elif (i - peaks[-1]) < min_sep and env[i] > env[peaks[-1]]:
                peaks[-1] = i
                valley_since_last = env[i]
    return [round(idx / sample_rate, 4) for idx in peaks]


def apply_stereo_echo(
    left: list[float],
    right: list[float],
    tail_scale: float = 1.0,
) -> tuple[list[float], list[float]]:
    if tail_scale <= 0.35:
        return left, right
    taps = (
        (int(0.067 * SAMPLE_RATE), 0.22 * min(1.6, tail_scale), True),
        (int(0.113 * SAMPLE_RATE), 0.14 * min(1.6, tail_scale), False),
        (int(0.179 * SAMPLE_RATE), 0.09 * min(1.6, tail_scale), True),
    )
    extra = int(0.22 * min(2.0, tail_scale) * SAMPLE_RATE)
    total = len(left) + extra
    out_l = left + [0.0] * extra
    out_r = right + [0.0] * extra
    for delay, gain, cross in taps:
        for i in range(len(left)):
            if i + delay < total:
                if cross:
                    out_l[i + delay] += right[i] * gain
                    out_r[i + delay] += left[i] * gain
                else:
                    out_l[i + delay] += left[i] * gain
                    out_r[i + delay] += right[i] * gain
    # Smooth final 15ms fade
    fade_n = min(total, int(0.015 * SAMPLE_RATE))
    for k in range(fade_n):
        f = k / max(1, fade_n)
        out_l[total - 1 - k] *= f
        out_r[total - 1 - k] *= f
    return out_l, out_r


def write_stereo_wav(
    path: Path,
    left: list[float],
    right: list[float],
    peak_target: float = 0.89,
) -> tuple[int, float]:
    n = min(len(left), len(right))
    peak = max(
        max((abs(left[i]) for i in range(n)), default=0.0),
        max((abs(right[i]) for i in range(n)), default=0.0),
        1e-9,
    )
    gain = peak_target / peak
    norm_denom = math.tanh(1.08)
    buf = bytearray(n * 4)
    for i in range(n):
        lv = math.tanh(left[i] * gain * 1.08) / norm_denom * peak_target
        rv = math.tanh(right[i] * gain * 1.08) / norm_denom * peak_target
        li = max(-32767, min(32767, int(round(lv * 32767.0))))
        ri = max(-32767, min(32767, int(round(rv * 32767.0))))
        struct.pack_into("<hh", buf, i * 4, li, ri)

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(bytes(buf))
    return path.stat().st_size, round(n / SAMPLE_RATE, 3)


def read_stereo_wav(path: Path) -> tuple[list[float], list[float]]:
    with wave.open(str(path), "rb") as wf:
        ch = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    vals = struct.unpack(f"<{len(raw) // 2}h", raw)
    if ch == 2:
        return (
            [vals[i] / 32768.0 for i in range(0, len(vals), 2)],
            [vals[i + 1] / 32768.0 for i in range(0, len(vals), 2)],
        )
    mono = [v / 32768.0 for v in vals]
    return mono, list(mono)


def _note_attack(t: float, base_attack: float, warm_night: bool) -> float:
    att_s = max(0.010, base_attack) if warm_night else base_attack
    if t >= att_s:
        return 1.0
    if warm_night:
        return 0.5 * (1.0 - math.cos(math.pi * (t / att_s)))
    return min(1.0, t / max(0.0015, att_s))


def _synth_fm_bell(
    freqs: list[float],
    duration: float,
    params: SoundSynthesisParams,
    stagger: float = 0.045,
    note_offsets: list[float] | None = None,
) -> tuple[list[float], list[float]]:
    eff_stagger = stagger / params.tempo_scale
    last_onset = (
        (note_offsets[-1] / params.tempo_scale)
        if note_offsets
        else ((len(freqs) - 1) * eff_stagger)
    )
    eff_dur = max(0.18, duration * (0.65 + 0.35 * params.reverb_tail), last_onset + 0.28)
    n = int(SAMPLE_RATE * eff_dur)
    left = [0.0] * n
    right = [0.0] * n
    decay = (18.0 if params.warm_night_mode else 3.8) / max(0.4, params.reverb_tail)
    mod_idx = (0.85 if params.warm_night_mode else 2.5) * params.brightness

    for vi, f0 in enumerate(freqs):
        onset_s = (
            (note_offsets[vi] / params.tempo_scale)
            if (note_offsets and vi < len(note_offsets))
            else (vi * eff_stagger)
        )
        start = min(n - 1, int(onset_s * SAMPLE_RATE))
        pan = 0.30 + 0.40 * (vi / max(1, len(freqs) - 1))
        gl = math.cos(pan * math.pi * 0.5)
        gr = math.sin(pan * math.pi * 0.5)
        fl = f0 * 0.9991
        fr = f0 * 1.0009
        base_att = 0.004 / params.punch
        for i in range(start, n):
            t = (i - start) / SAMPLE_RATE
            att = _note_attack(t, base_att, params.warm_night_mode)
            env = att * math.exp(-decay * t)
            transient = (
                0.0
                if params.warm_night_mode
                else 0.25
                * params.punch
                * math.sin(2.0 * math.pi * f0 * 5.0 * t)
                * math.exp(-42.0 * t)
            )
            m_sig = mod_idx * math.exp(-decay * 1.8 * t) * math.sin(2.0 * math.pi * f0 * 2.0 * t)
            sl = math.sin(2.0 * math.pi * fl * t + m_sig) + transient
            sr = math.sin(2.0 * math.pi * fr * t + m_sig) + transient
            left[i] += env * sl * gl
            right[i] += env * sr * gr
    echo_tail = 0.25 if params.warm_night_mode else params.reverb_tail
    return apply_stereo_echo(left, right, echo_tail)


def _synth_ritual_gong(
    freqs: list[float],
    duration: float,
    params: SoundSynthesisParams,
    note_offsets: list[float] | None = None,
) -> tuple[list[float], list[float]]:
    strike_freqs = [f * 0.5 for f in (freqs if params.warm_night_mode else freqs[:1])]
    last_onset = (
        (note_offsets[min(len(strike_freqs) - 1, len(note_offsets) - 1)] / params.tempo_scale)
        if (note_offsets and len(strike_freqs) > 1)
        else 0.0
    )
    eff_dur = max(0.25, duration * (0.7 + 0.3 * params.reverb_tail), last_onset + 0.34)
    n = int(SAMPLE_RATE * eff_dur)
    bright_scale = params.brightness * (0.45 if params.warm_night_mode else 1.0)
    partials = (
        (1.0, 1.0, 2.2),
        (1.414, 0.62 * bright_scale, 2.9),
        (1.732, 0.48 * bright_scale, 3.5),
        (2.38, 0.38 * bright_scale, 4.3),
        (3.11, 0.26 * bright_scale, 5.4),
    )
    left = [0.0] * n
    right = [0.0] * n
    strike_decay = 2.1 if len(strike_freqs) > 1 else 1.0
    for vi, f0 in enumerate(strike_freqs):
        onset_s = (
            (note_offsets[vi] / params.tempo_scale)
            if (note_offsets and vi < len(note_offsets))
            else 0.0
        )
        start = min(n - 1, int(onset_s * SAMPLE_RATE))
        for i in range(start, n):
            t = (i - start) / SAMPLE_RATE
            att = _note_attack(t, 0.004, params.warm_night_mode)
            sub_f = f0 * 0.5 * (1.0 + 0.65 * params.punch * math.exp(-34.0 * t))
            sub = 0.85 * params.punch * math.sin(2.0 * math.pi * sub_f * t) * math.exp(-7.5 * t)
            sl = sub
            sr = sub
            for ratio, amp, dec in partials:
                e = amp * math.exp(-(dec * strike_decay / max(0.45, params.reverb_tail)) * t)
                sl += e * math.sin(2.0 * math.pi * (f0 * ratio - 0.55) * t)
                sr += e * math.sin(2.0 * math.pi * (f0 * ratio + 0.55) * t + 0.35)
            left[i] += att * sl
            right[i] += att * sr
    echo_tail = 0.25 if params.warm_night_mode else params.reverb_tail
    return apply_stereo_echo(left, right, echo_tail)


def _synth_cyber_lead(
    freqs: list[float],
    duration: float,
    params: SoundSynthesisParams,
) -> tuple[list[float], list[float]]:
    step = (
        ((0.095 if params.warm_night_mode else 0.065) / params.tempo_scale)
        if len(freqs) > 1
        else duration
    )
    eff_dur = max(duration, len(freqs) * step + 0.22)
    n = int(SAMPLE_RATE * eff_dur)
    left = [0.0] * n
    right = [0.0] * n
    max_h = 3 if params.warm_night_mode else max(3, min(11, int(round(6 * params.brightness))))
    for i in range(n):
        t = i / SAMPLE_RATE
        idx = min(len(freqs) - 1, int(t / max(0.02, step)))
        f0 = freqs[idx]
        local_t = t - idx * step
        tail_t = max(0.0, t - (len(freqs) - 1) * step)
        note_gate = (
            _note_attack(local_t, 0.008, True) * math.exp(-7.5 * local_t)
            if params.warm_night_mode
            else min(1.0, t / 0.003) * (math.exp(-4.2 * tail_t) if idx == len(freqs) - 1 else 0.9)
        )
        sl = 0.0
        sr = 0.0
        # Detuned band-limited sawtooth pair + sub bass
        for k in range(1, max_h + 1):
            filt = math.exp(-0.22 * k / params.brightness)
            amp = (1.0 / k) * filt
            sl += amp * math.sin(2.0 * math.pi * k * (f0 * 0.996) * t)
            sr += amp * math.sin(2.0 * math.pi * k * (f0 * 1.004) * t)
        sub = 0.45 * params.punch * math.sin(2.0 * math.pi * (f0 * 0.5) * t)
        left[i] = note_gate * (sl + sub)
        right[i] = note_gate * (sr + sub)
    echo_tail = 0.25 if params.warm_night_mode else params.reverb_tail
    return apply_stereo_echo(left, right, echo_tail)


def _synth_arcade_retro(
    freqs: list[float],
    params: SoundSynthesisParams,
) -> tuple[list[float], list[float]]:
    step = (0.088 if params.warm_night_mode else 0.052) / params.tempo_scale
    duration = len(freqs) * step + 0.22
    n = int(SAMPLE_RATE * duration)
    left = [0.0] * n
    right = [0.0] * n
    harm_cap = 4 if params.warm_night_mode else 8
    for i in range(n):
        t = i / SAMPLE_RATE
        idx = min(len(freqs) - 1, int(t / step))
        f = freqs[idx]
        local_t = t - idx * step
        tail_t = max(0.0, t - (len(freqs) - 1) * step)
        if params.warm_night_mode:
            env = _note_attack(local_t, 0.008, True) * math.exp(-9.0 * local_t)
        else:
            env = min(1.0, local_t / 0.002) * (
                math.exp(-5.0 * tail_t) if idx == len(freqs) - 1 else 0.86
            )
        duty = 0.5 if params.warm_night_mode else (0.25 if idx % 2 == 0 else 0.5)
        pl = sum(
            (2.0 / (k * math.pi))
            * math.sin(k * math.pi * duty)
            * math.cos(2.0 * math.pi * k * f * t)
            for k in range(1, harm_cap)
        )
        pr = sum(
            (2.0 / (k * math.pi))
            * math.sin(k * math.pi * duty)
            * math.cos(2.0 * math.pi * k * (f * 1.0015) * t)
            for k in range(1, harm_cap)
        )
        tri = 0.35 * params.punch * math.sin(2.0 * math.pi * (f * 0.5) * t)
        left[i] = env * (pl + tri)
        right[i] = env * (pr + tri)
    return apply_stereo_echo(
        left,
        right,
        params.reverb_tail * (0.35 if params.warm_night_mode else 0.6),
    )


def _synth_zen_tick(
    freqs: list[float],
    duration: float,
    params: SoundSynthesisParams,
    note_offsets: list[float] | None = None,
) -> tuple[list[float], list[float]]:
    last_onset = (
        (note_offsets[-1] / params.tempo_scale)
        if note_offsets
        else ((len(freqs) - 1) * 0.075 / params.tempo_scale)
    )
    eff_dur = max(duration, last_onset + 0.24)
    n = int(SAMPLE_RATE * eff_dur)
    left = [0.0] * n
    right = [0.0] * n
    for vi, f0 in enumerate(freqs):
        onset_s = (
            (note_offsets[vi] / params.tempo_scale)
            if (note_offsets and vi < len(note_offsets))
            else (vi * 0.075 / params.tempo_scale)
        )
        start = min(n - 1, int(onset_s * SAMPLE_RATE))
        for i in range(start, n):
            t = (i - start) / SAMPLE_RATE
            att = _note_attack(t, 0.002, params.warm_night_mode)
            env = att * math.exp(-13.0 * t / max(0.5, params.reverb_tail))
            tine = (
                0.0
                if params.warm_night_mode
                else 0.30
                * params.brightness
                * math.sin(2.0 * math.pi * f0 * 4.0 * t)
                * math.exp(-42.0 * t)
            )
            body = math.sin(2.0 * math.pi * f0 * t) + 0.4 * math.sin(
                2.0 * math.pi * f0 * 1.5 * t
            ) * math.exp(-18.0 * t)
            left[i] += env * (body + tine)
            right[i] += env * (body * 0.96 + tine * 1.04)
    return left, right


_TIER_DEGREE_AND_RHYTHM: dict[str, tuple[list[int], int, float, list[float]]] = {
    "s_plus": ([0, 2, 4, 6, 7, 9], 5, 1.05, [0.0, 0.095, 0.190, 0.285, 0.380, 0.505]),
    "s": ([0, 2, 4, 7], 5, 0.88, [0.0, 0.105, 0.210, 0.340]),
    "a": ([0, 4, 7], 5, 0.62, [0.0, 0.115, 0.240]),
    "b": ([0, 4], 5, 0.40, [0.0, 0.130]),
    "c": ([0], 5, 0.24, [0.0]),
    "d": ([0], 4, 0.20, [0.0]),
    "build_upgrade": ([0, 3, 4, 7], 5, 0.78, [0.0, 0.072, 0.144, 0.305]),
    "waystone_high": ([0, 4, 6, 7], 4, 0.75, [0.0, 0.135, 0.270, 0.405]),
    "waystone_prog": ([0, 4], 4, 0.42, [0.0, 0.145]),
    "chancing": ([2, 4, 7], 5, 0.52, [0.0, 0.068, 0.205]),
    "gold_high": ([0, 4, 7], 5, 0.45, [0.0, 0.080, 0.160]),
}


def synthesize_tier_waveform(
    style: SoundPackStyle,
    tier: str,
    params: SoundSynthesisParams,
) -> tuple[list[float], list[float]]:
    degrees, octave, dur, offsets = _TIER_DEGREE_AND_RHYTHM.get(
        tier, ([0, 4], 5, 0.40, [0.0, 0.125])
    )
    eff_octave = max(3, octave - 1) if (params.warm_night_mode and octave >= 5) else octave
    freqs = scale_frequencies(params.musical_key, params.scale, degrees, octave=eff_octave)
    rhythm_offsets = offsets if params.warm_night_mode else None

    if style == SoundPackStyle.RITUAL_GONG:
        left, right = _synth_ritual_gong(freqs, dur * 1.15, params, note_offsets=rhythm_offsets)
    elif style == SoundPackStyle.CYBER_SYNTH:
        left, right = _synth_cyber_lead(freqs, dur, params)
    elif style == SoundPackStyle.ARCADE_RETRO:
        left, right = _synth_arcade_retro(freqs, params)
    elif style == SoundPackStyle.ZEN_MINIMAL:
        zen_freqs = freqs if params.warm_night_mode else freqs[: max(1, min(len(freqs), 4))]
        zen_offsets = offsets[: len(zen_freqs)]
        left, right = _synth_zen_tick(
            zen_freqs,
            min(0.85, max(0.24, dur * 0.85)),
            params,
            note_offsets=zen_offsets,
        )
    else:
        left, right = _synth_fm_bell(
            freqs,
            dur,
            params,
            note_offsets=rhythm_offsets,
        )

    eff_lp = _effective_lowpass_hz(params)
    if eff_lp is not None:
        left, right = apply_lowpass_filter(left, right, eff_lp)
    return left, right


def _try_render_tts_wav(
    phrase: str,
    out_wav: Path,
    params: SoundSynthesisParams,
    ffmpeg_bin: str | None,
) -> str | None:
    # 1. Local OpenAI-compatible audio proxy (`127.0.0.1:18455/v1/audio/speech`)
    if params.prefer_local_ai_tts and ffmpeg_bin:
        tmp_mp3 = out_wav.with_suffix(".tts_tmp.mp3")
        try:
            ai_voice = (
                params.voice_name
                if params.voice_name in ("alloy", "echo", "fable", "onyx", "nova", "shimmer")
                else "onyx"
            )
            api_key = (
                os.environ.get("QUOTASWITCH_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or "quotaswitch-local"
            )
            req = urllib.request.Request(
                "http://127.0.0.1:18455/v1/audio/speech",
                data=json.dumps(
                    {
                        "model": "gpt-4o-mini-tts",
                        "voice": ai_voice,
                        "input": phrase,
                        "response_format": "mp3",
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                if resp.status == 200:
                    payload = resp.read()
                    if len(payload) > 512:
                        tmp_mp3.write_bytes(payload)
                        subprocess.run(
                            [
                                ffmpeg_bin,
                                "-y",
                                "-loglevel",
                                "error",
                                "-i",
                                str(tmp_mp3),
                                "-ar",
                                str(SAMPLE_RATE),
                                "-ac",
                                "2",
                                "-sample_fmt",
                                "s16",
                                str(out_wav),
                            ],
                            check=True,
                            timeout=8.0,
                        )
                        return f"openai_local_tts:{ai_voice}"
        except Exception:
            pass
        finally:
            tmp_mp3.unlink(missing_ok=True)

    # 2. macOS offline `say` + `afconvert`
    say_bin = shutil.which("say")
    afconvert_bin = shutil.which("afconvert")
    if say_bin and afconvert_bin:
        tmp_aiff = out_wav.with_suffix(".say_tmp.aiff")
        try:
            default_mac_voice = (
                "Tingting" if (params.voice_language == "zh" or _has_cjk(phrase)) else "Samantha"
            )
            mac_voice = (
                params.voice_name
                if params.voice_name and params.voice_name not in ("alloy", "onyx", "nova")
                else default_mac_voice
            )
            cmd = [say_bin, "-v", mac_voice, "-r", "215", "-o", str(tmp_aiff), phrase]
            res = subprocess.run(cmd, capture_output=True, timeout=8.0)
            if res.returncode != 0:
                subprocess.run(
                    [say_bin, "-r", "215", "-o", str(tmp_aiff), phrase], check=True, timeout=8.0
                )
                mac_voice = "default"
            subprocess.run(
                [
                    afconvert_bin,
                    "-f",
                    "WAVE",
                    "-d",
                    f"LEI16@{SAMPLE_RATE}",
                    "-c",
                    "2",
                    str(tmp_aiff),
                    str(out_wav),
                ],
                check=True,
                timeout=8.0,
            )
            return f"macos_say:{mac_voice}"
        except Exception:
            pass
        finally:
            tmp_aiff.unlink(missing_ok=True)

    return None


def _mix_stinger_with_voice(
    stinger_lr: tuple[list[float], list[float]],
    voice_wav: Path,
    offset_seconds: float = 0.14,
) -> tuple[list[float], list[float]]:
    cl, cr = stinger_lr
    vl, vr = read_stereo_wav(voice_wav)
    thresh = 0.012
    s_idx = next((i for i in range(len(vl)) if abs(vl[i]) > thresh or abs(vr[i]) > thresh), 0)
    e_idx = next(
        (i for i in range(len(vl) - 1, -1, -1) if abs(vl[i]) > thresh or abs(vr[i]) > thresh),
        len(vl) - 1,
    )
    vl = vl[max(0, s_idx - 350) : min(len(vl), e_idx + 1100)]
    vr = vr[max(0, s_idx - 350) : min(len(vr), e_idx + 1100)]

    offset = int(offset_seconds * SAMPLE_RATE)
    total = max(len(cl), offset + len(vl))
    out_l = [0.0] * total
    out_r = [0.0] * total
    for i in range(len(cl)):
        duck = 0.38 if offset <= i < offset + len(vl) else 1.0
        out_l[i] += cl[i] * duck
        out_r[i] += cr[i] * duck
    for j in range(len(vl)):
        out_l[offset + j] += vl[j] * 1.18
        out_r[offset + j] += vr[j] * 1.18
    return out_l, out_r


def _synth_prosodic_callout_fallback(phrase: str, out_wav: Path) -> str:
    """Pure-Python syllable formant fallback when neither OpenAI TTS nor macOS `say` exists."""
    words = [w for w in re.findall(r"[A-Za-z0-9]+", phrase) if w]
    syllables = max(2, min(10, sum(max(1, len(re.findall(r"[aeiouyAEIOUY]+", w))) for w in words)))
    syl_dur = 0.115
    total_n = int(SAMPLE_RATE * (syllables * syl_dur + 0.10))
    left = [0.0] * total_n
    right = [0.0] * total_n
    formants = ((480.0, 1380.0), (620.0, 1720.0), (420.0, 1180.0), (550.0, 1560.0))
    for si in range(syllables):
        start = int(si * syl_dur * SAMPLE_RATE)
        end = min(total_n, start + int(0.095 * SAMPLE_RATE))
        f0 = 155.0 + 14.0 * ((si + len(phrase)) % 4)
        f1, f2 = formants[si % len(formants)]
        for i in range(start, end):
            t = (i - start) / SAMPLE_RATE
            env = math.sin(math.pi * (t / 0.095)) ** 0.75
            glottal = (
                0.55 * math.sin(2.0 * math.pi * f0 * t)
                + 0.28 * math.sin(2.0 * math.pi * f1 * t)
                + 0.17 * math.sin(2.0 * math.pi * f2 * t)
            )
            left[i] += env * glottal * 0.9
            right[i] += env * glottal * 0.9
    write_stereo_wav(out_wav, left, right, peak_target=0.75)
    return "pure_python_prosodic_tts"


class SoundPackGenerator:
    """Generates 44.1kHz 16-bit stereo `.wav` synth packs and hybrid voice+chime callouts."""

    def generate_pack(
        self,
        output_root: Path,
        *,
        style: SoundPackStyle | str = SoundPackStyle.CELESTIAL_CHIME,
        pack_id: str | None = None,
        params: SoundSynthesisParams | None = None,
        custom_callouts: Mapping[str, str] | None = None,
        warm_night_mode: bool | None = None,
        lowpass_hz: float | None = None,
        tiers: tuple[str, ...] = SOUND_PACK_TIERS,
    ) -> SoundPackManifest:
        style_enum = SoundPackStyle(style) if isinstance(style, str) else style
        base_cfg = params.model_copy(deep=True) if params is not None else SoundSynthesisParams()
        explicit_callouts = _normalize_callout_map(base_cfg.custom_callouts)
        if custom_callouts:
            explicit_callouts.update(_normalize_callout_map(custom_callouts))
        base_cfg.custom_callouts = explicit_callouts
        if warm_night_mode is not None:
            base_cfg.warm_night_mode = warm_night_mode
        if lowpass_hz is not None:
            base_cfg.lowpass_hz = lowpass_hz

        cfg = base_cfg
        resolved_id = pack_id or f"poe2brain_{style_enum.value}"
        if not resolved_id.startswith("poe2brain_"):
            resolved_id = f"poe2brain_{resolved_id}"

        pack_dir = output_root / resolved_id
        pack_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg_bin = shutil.which("ffmpeg")

        default_callouts = (
            DEFAULT_TIER_CALLOUTS_ZH
            if cfg.voice_language == "zh" or any(_has_cjk(v) for v in explicit_callouts.values())
            else DEFAULT_TIER_CALLOUTS
        )
        callouts = (
            {**default_callouts, **explicit_callouts}
            if style_enum == SoundPackStyle.VOICE_CALLOUT
            else dict(explicit_callouts)
        )
        eff_tiers_list = list(tiers)
        if tiers == SOUND_PACK_TIERS and explicit_callouts:
            for extra_tier in explicit_callouts:
                if extra_tier not in eff_tiers_list:
                    eff_tiers_list.append(extra_tier)

        eff_lp = _effective_lowpass_hz(cfg)
        peak_target = min(cfg.peak_ceiling, 0.74) if cfg.warm_night_mode else cfg.peak_ceiling
        clips: dict[str, GeneratedSoundClip] = {}

        for tier in eff_tiers_list:
            base_synth_style = (
                SoundPackStyle.CELESTIAL_CHIME
                if style_enum == SoundPackStyle.VOICE_CALLOUT
                else style_enum
            )
            synth_lr = synthesize_tier_waveform(base_synth_style, tier, cfg)
            backend = f"pure_python_synth:{base_synth_style.value}"
            spoken_line: str | None = None

            should_speak = style_enum == SoundPackStyle.VOICE_CALLOUT or tier in explicit_callouts
            if should_speak and tier in callouts and callouts[tier].strip():
                phrase = callouts[tier].strip()
                tmp_voice = pack_dir / f"_tmp_voice_{tier}.wav"
                tts_backend = _try_render_tts_wav(phrase, tmp_voice, cfg, ffmpeg_bin)
                if not tts_backend or not tmp_voice.is_file():
                    tts_backend = _synth_prosodic_callout_fallback(phrase, tmp_voice)
                if tts_backend and tmp_voice.is_file():
                    synth_lr = _mix_stinger_with_voice(synth_lr, tmp_voice)
                    if eff_lp is not None:
                        synth_lr = apply_lowpass_filter(synth_lr[0], synth_lr[1], eff_lp)
                    backend = f"{tts_backend}+chime"
                    spoken_line = phrase
                tmp_voice.unlink(missing_ok=True)

            hf_ratio = measure_high_frequency_energy_ratio(synth_lr[0], cutoff_hz=2500.0)
            wav_name = f"{tier}.wav"
            wav_path = pack_dir / wav_name
            wav_bytes, dur_s = write_stereo_wav(
                wav_path,
                synth_lr[0],
                synth_lr[1],
                peak_target=peak_target,
            )

            mp3_name: str | None = None
            mp3_bytes: int | None = None
            if cfg.export_mp3 and ffmpeg_bin:
                cand_mp3 = pack_dir / f"{tier}.mp3"
                try:
                    subprocess.run(
                        [
                            ffmpeg_bin,
                            "-y",
                            "-loglevel",
                            "error",
                            "-i",
                            str(wav_path),
                            "-codec:a",
                            "libmp3lame",
                            "-b:a",
                            "192k",
                            str(cand_mp3),
                        ],
                        check=True,
                        timeout=8.0,
                    )
                    mp3_name = cand_mp3.name
                    mp3_bytes = cand_mp3.stat().st_size
                except Exception:
                    cand_mp3.unlink(missing_ok=True)

            clips[tier] = GeneratedSoundClip(
                tier=tier,
                filename_wav=wav_name,
                filename_mp3=mp3_name,
                relative_filter_path=f"{resolved_id}/{wav_name}",
                duration_seconds=dur_s,
                bytes_wav=wav_bytes,
                bytes_mp3=mp3_bytes,
                sample_rate=SAMPLE_RATE,
                channels=2,
                bits=16,
                backend_used=backend,
                callout_text=spoken_line,
                lowpass_hz_applied=eff_lp,
                hf_energy_ratio_above_2500hz=hf_ratio,
            )

        manifest = SoundPackManifest(
            pack_id=resolved_id,
            style=style_enum,
            pack_dir=str(pack_dir),
            params=cfg,
            clips=clips,
        )
        (pack_dir / "manifest.json").write_text(
            manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        return manifest
