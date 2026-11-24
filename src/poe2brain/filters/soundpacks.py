from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import urllib.request
import wave
from pathlib import Path

from poe2brain.models.filters import (
    GeneratedSoundClip,
    SoundPackManifest,
    SoundPackStyle,
    SoundSynthesisParams,
)

SAMPLE_RATE: int = 44100

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


def _synth_fm_bell(
    freqs: list[float],
    duration: float,
    params: SoundSynthesisParams,
    stagger: float = 0.045,
) -> tuple[list[float], list[float]]:
    eff_stagger = stagger / params.tempo_scale
    eff_dur = max(0.18, duration * (0.65 + 0.35 * params.reverb_tail))
    n = int(SAMPLE_RATE * eff_dur)
    left = [0.0] * n
    right = [0.0] * n
    decay = 3.8 / max(0.4, params.reverb_tail)
    mod_idx = 2.5 * params.brightness

    for vi, f0 in enumerate(freqs):
        start = int(vi * eff_stagger * SAMPLE_RATE)
        pan = 0.30 + 0.40 * (vi / max(1, len(freqs) - 1))
        gl = math.cos(pan * math.pi * 0.5)
        gr = math.sin(pan * math.pi * 0.5)
        fl = f0 * 0.9991
        fr = f0 * 1.0009
        for i in range(start, n):
            t = (i - start) / SAMPLE_RATE
            att = min(1.0, t / max(0.0015, 0.004 / params.punch))
            env = att * math.exp(-decay * t)
            transient = (
                0.25 * params.punch * math.sin(2.0 * math.pi * f0 * 5.0 * t) * math.exp(-42.0 * t)
            )
            m_sig = mod_idx * math.exp(-decay * 1.8 * t) * math.sin(2.0 * math.pi * f0 * 2.0 * t)
            sl = math.sin(2.0 * math.pi * fl * t + m_sig) + transient
            sr = math.sin(2.0 * math.pi * fr * t + m_sig) + transient
            left[i] += env * sl * gl
            right[i] += env * sr * gr
    return apply_stereo_echo(left, right, params.reverb_tail)


def _synth_ritual_gong(
    f0: float,
    duration: float,
    params: SoundSynthesisParams,
) -> tuple[list[float], list[float]]:
    eff_dur = max(0.25, duration * (0.7 + 0.3 * params.reverb_tail))
    n = int(SAMPLE_RATE * eff_dur)
    partials = (
        (1.0, 1.0, 2.2),
        (1.414, 0.62 * params.brightness, 2.9),
        (1.732, 0.48 * params.brightness, 3.5),
        (2.38, 0.38 * params.brightness, 4.3),
        (3.11, 0.26 * params.brightness, 5.4),
    )
    left = [0.0] * n
    right = [0.0] * n
    for i in range(n):
        t = i / SAMPLE_RATE
        att = min(1.0, t / 0.004)
        sub_f = f0 * 0.5 * (1.0 + 0.65 * params.punch * math.exp(-34.0 * t))
        sub = 0.85 * params.punch * math.sin(2.0 * math.pi * sub_f * t) * math.exp(-7.5 * t)
        sl = sub
        sr = sub
        for ratio, amp, dec in partials:
            e = amp * math.exp(-(dec / max(0.45, params.reverb_tail)) * t)
            sl += e * math.sin(2.0 * math.pi * (f0 * ratio - 0.55) * t)
            sr += e * math.sin(2.0 * math.pi * (f0 * ratio + 0.55) * t + 0.35)
        left[i] = att * sl
        right[i] = att * sr
    return apply_stereo_echo(left, right, params.reverb_tail)


def _synth_cyber_lead(
    freqs: list[float],
    duration: float,
    params: SoundSynthesisParams,
) -> tuple[list[float], list[float]]:
    step = (0.065 / params.tempo_scale) if len(freqs) > 1 else duration
    eff_dur = max(duration, len(freqs) * step + 0.22)
    n = int(SAMPLE_RATE * eff_dur)
    left = [0.0] * n
    right = [0.0] * n
    max_h = max(3, min(11, int(round(6 * params.brightness))))
    for i in range(n):
        t = i / SAMPLE_RATE
        idx = min(len(freqs) - 1, int(t / max(0.02, step)))
        f0 = freqs[idx]
        tail_t = max(0.0, t - (len(freqs) - 1) * step)
        env = min(1.0, t / 0.003) * (math.exp(-4.2 * tail_t) if idx == len(freqs) - 1 else 0.9)
        sl = 0.0
        sr = 0.0
        # Detuned band-limited sawtooth pair + sub bass
        for k in range(1, max_h + 1):
            filt = math.exp(-0.22 * k / params.brightness)
            amp = (1.0 / k) * filt
            sl += amp * math.sin(2.0 * math.pi * k * (f0 * 0.996) * t)
            sr += amp * math.sin(2.0 * math.pi * k * (f0 * 1.004) * t)
        sub = 0.45 * params.punch * math.sin(2.0 * math.pi * (f0 * 0.5) * t)
        left[i] = env * (sl + sub)
        right[i] = env * (sr + sub)
    return apply_stereo_echo(left, right, params.reverb_tail)


def _synth_arcade_retro(
    freqs: list[float],
    params: SoundSynthesisParams,
) -> tuple[list[float], list[float]]:
    step = 0.052 / params.tempo_scale
    duration = len(freqs) * step + 0.22
    n = int(SAMPLE_RATE * duration)
    left = [0.0] * n
    right = [0.0] * n
    for i in range(n):
        t = i / SAMPLE_RATE
        idx = min(len(freqs) - 1, int(t / step))
        f = freqs[idx]
        local_t = t - idx * step
        tail_t = max(0.0, t - (len(freqs) - 1) * step)
        env = min(1.0, local_t / 0.002) * (
            math.exp(-5.0 * tail_t) if idx == len(freqs) - 1 else 0.86
        )
        duty = 0.25 if idx % 2 == 0 else 0.5
        pl = sum(
            (2.0 / (k * math.pi))
            * math.sin(k * math.pi * duty)
            * math.cos(2.0 * math.pi * k * f * t)
            for k in range(1, 8)
        )
        pr = sum(
            (2.0 / (k * math.pi))
            * math.sin(k * math.pi * duty)
            * math.cos(2.0 * math.pi * k * (f * 1.0015) * t)
            for k in range(1, 8)
        )
        tri = 0.35 * params.punch * math.sin(2.0 * math.pi * (f * 0.5) * t)
        left[i] = env * (pl + tri)
        right[i] = env * (pr + tri)
    return apply_stereo_echo(left, right, params.reverb_tail * 0.6)


def _synth_zen_tick(
    freqs: list[float],
    duration: float,
    params: SoundSynthesisParams,
) -> tuple[list[float], list[float]]:
    n = int(SAMPLE_RATE * duration)
    left = [0.0] * n
    right = [0.0] * n
    for vi, f0 in enumerate(freqs):
        start = int(vi * 0.032 * SAMPLE_RATE / params.tempo_scale)
        for i in range(start, n):
            t = (i - start) / SAMPLE_RATE
            env = min(1.0, t / 0.002) * math.exp(-13.0 * t / max(0.5, params.reverb_tail))
            tine = (
                0.30
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


def synthesize_tier_waveform(
    style: SoundPackStyle,
    tier: str,
    params: SoundSynthesisParams,
) -> tuple[list[float], list[float]]:
    deg_map: dict[str, tuple[list[int], int, float]] = {
        "s_plus": ([0, 2, 4, 6, 7, 9], 5, 1.05),
        "s": ([0, 2, 4, 7], 5, 0.88),
        "a": ([0, 4, 7], 5, 0.62),
        "b": ([0, 4], 5, 0.40),
        "c": ([0], 5, 0.24),
        "build_upgrade": ([0, 3, 4, 7], 5, 0.78),
        "waystone_high": ([0, 4, 6, 7], 4, 0.75),
        "chancing": ([2, 4, 7], 5, 0.52),
    }
    degrees, octave, dur = deg_map.get(tier, ([0, 4], 5, 0.40))
    freqs = scale_frequencies(params.musical_key, params.scale, degrees, octave=octave)

    if style == SoundPackStyle.RITUAL_GONG:
        return _synth_ritual_gong(freqs[0] * 0.5, dur * 1.15, params)
    if style == SoundPackStyle.CYBER_SYNTH:
        return _synth_cyber_lead(freqs, dur, params)
    if style == SoundPackStyle.ARCADE_RETRO:
        return _synth_arcade_retro(freqs, params)
    if style == SoundPackStyle.ZEN_MINIMAL:
        return _synth_zen_tick(freqs[:2], min(0.42, dur * 0.65), params)
    return _synth_fm_bell(freqs, dur, params)


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
                headers={"Content-Type": "application/json", "Authorization": "Bearer local"},
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
            mac_voice = (
                params.voice_name
                if params.voice_name and params.voice_name not in ("alloy", "onyx", "nova")
                else "Samantha"
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


class SoundPackGenerator:
    """Generates 44.1kHz 16-bit stereo `.wav` synth packs and hybrid voice+chime callouts."""

    def generate_pack(
        self,
        output_root: Path,
        *,
        style: SoundPackStyle = SoundPackStyle.CELESTIAL_CHIME,
        pack_id: str | None = None,
        params: SoundSynthesisParams | None = None,
        tiers: tuple[str, ...] = SOUND_PACK_TIERS,
    ) -> SoundPackManifest:
        cfg = params or SoundSynthesisParams()
        resolved_id = pack_id or f"poe2brain_{style.value}"
        if not resolved_id.startswith("poe2brain_"):
            resolved_id = f"poe2brain_{resolved_id}"

        pack_dir = output_root / resolved_id
        pack_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg_bin = shutil.which("ffmpeg")

        callouts = {**DEFAULT_TIER_CALLOUTS, **cfg.custom_callouts}
        clips: dict[str, GeneratedSoundClip] = {}

        for tier in tiers:
            base_synth_style = (
                SoundPackStyle.CELESTIAL_CHIME if style == SoundPackStyle.VOICE_CALLOUT else style
            )
            synth_lr = synthesize_tier_waveform(base_synth_style, tier, cfg)
            backend = f"pure_python_synth:{base_synth_style.value}"
            spoken_line: str | None = None

            if (
                style == SoundPackStyle.VOICE_CALLOUT
                and tier in callouts
                and callouts[tier].strip()
            ):
                phrase = callouts[tier].strip()
                tmp_voice = pack_dir / f"_tmp_voice_{tier}.wav"
                tts_backend = _try_render_tts_wav(phrase, tmp_voice, cfg, ffmpeg_bin)
                if tts_backend and tmp_voice.is_file():
                    synth_lr = _mix_stinger_with_voice(synth_lr, tmp_voice)
                    backend = f"{tts_backend}+chime"
                    spoken_line = phrase
                tmp_voice.unlink(missing_ok=True)

            wav_name = f"{tier}.wav"
            wav_path = pack_dir / wav_name
            wav_bytes, dur_s = write_stereo_wav(wav_path, synth_lr[0], synth_lr[1])

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
            )

        manifest = SoundPackManifest(
            pack_id=resolved_id,
            style=style,
            pack_dir=str(pack_dir),
            params=cfg,
            clips=clips,
        )
        (pack_dir / "manifest.json").write_text(
            manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        return manifest
