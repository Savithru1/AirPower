"""Vibration features from a raw accelerometer waveform (HUMS gearbox / bearing channels).

Copied unchanged from AeroTwin (sih26054_v2/edge/dsp.py), where it ran on the onboard edge
computer. In AirPower it computes the same features from HUMS waveform downloads; the
envelope-modulation feature was chosen there on real CWRU bearing faults.

Original description: runs at 1 Hz per engine (0.5 s window, 8192 Hz).
Order amplitudes are tracked from the ECU crank speed and the governor prop speed
(order tracking), so the features stay meaningful across the rpm range.
Batched over rows for throughput; one row = one second = one engine.

Features (contract v2.1): Vib_RMS, Vib_Kurtosis, Vib_1P, Vib_05E, Vib_2E, Vib_GMF, Vib_EnvMod.
"""
import numpy as np

FS = 8192
GMF_TEETH = 27


def _band_peak(spec, freqs, f, rel=0.02, min_bins=2):
    """Peak amplitude within +-max(rel*f, min_bins) of frequency f (per row)."""
    df = freqs[1] - freqs[0]
    out = np.zeros(spec.shape[0])
    for i in range(spec.shape[0]):
        if f[i] <= df:
            continue
        half = max(rel * f[i], min_bins * df)
        lo = np.searchsorted(freqs, f[i] - half)
        hi = np.searchsorted(freqs, f[i] + half, side="right")
        out[i] = spec[i, lo:hi].max() if hi > lo else 0.0
    return out


def _band_rms(spec, freqs, f, half_hz):
    out = np.zeros(spec.shape[0])
    for i in range(spec.shape[0]):
        if f[i] <= 0:
            continue
        lo = np.searchsorted(freqs, f[i] - half_hz[i])
        hi = np.searchsorted(freqs, f[i] + half_hz[i], side="right")
        out[i] = np.sqrt(np.sum(spec[i, lo:hi] ** 2) / 1.5)   # /1.5: Hann energy correction
    return out


ENV_BAND_HZ = (1500.0, 4000.0)      # structural resonance band excited by bearing / tooth impacts


def envelope_modulation(x):
    """Envelope modulation index (contract v2.1 Vib_EnvMod): std / mean of the envelope of the
    1.5-4 kHz band (analytic signal by FFT). Impacts ring the structure in this band and
    modulate its envelope; steady order content and broadband noise do not.

    Chosen on REAL data (tools/calibrate_vibration.py, docs/IMPLEMENTATION_PLAN.md B4): on
    CWRU bearing faults resampled to the edge rate it separates all six defects, including the
    0.007 in. ball defect that plain kurtosis misses (directional AUC 1.00 vs 0.49). An
    envelope-SPECTRUM kurtosis, tried first, fell with damage on CWRU (healthy spectrum already
    peaky) and was rejected."""
    M, n = x.shape
    X = np.fft.rfft(x, axis=1)
    f = np.fft.rfftfreq(n, 1.0 / FS)
    band = np.flatnonzero((f >= ENV_BAND_HZ[0]) & (f <= ENV_BAND_HZ[1]))
    Z = np.zeros((M, n), dtype=np.complex64)
    Z[:, band] = 2.0 * X[:, band]
    env = np.abs(np.fft.ifft(Z, axis=1))
    mu = env.mean(axis=1)
    return np.where(mu > 1e-12, env.std(axis=1) / np.maximum(mu, 1e-12), 0.0)


def features(x, rpm, prop_rpm):
    """x: [M, N] waveform (g); rpm, prop_rpm: [M].  Returns dict of [M] arrays."""
    x = np.asarray(x, dtype=np.float32)
    x = x - x.mean(axis=1, keepdims=True)
    n = x.shape[1]
    rms = np.sqrt(np.mean(x * x, axis=1))
    m4 = np.mean(x ** 4, axis=1)
    kurt = np.where(rms > 1e-6, m4 / np.maximum(rms, 1e-12) ** 4, 3.0)
    win = np.hanning(n).astype(np.float32)
    spec = np.abs(np.fft.rfft(x * win[None, :], axis=1)) * 2.0 / win.sum()
    freqs = np.fft.rfftfreq(n, 1.0 / FS)
    f1e = np.asarray(rpm) / 60.0
    f1p = np.asarray(prop_rpm) / 60.0
    return {
        "Vib_RMS": rms,
        "Vib_Kurtosis": kurt,
        "Vib_1P": _band_peak(spec, freqs, f1p),
        "Vib_05E": _band_peak(spec, freqs, 0.5 * f1e),
        "Vib_2E": _band_peak(spec, freqs, 2.0 * f1e),
        "Vib_GMF": _band_rms(spec, freqs, GMF_TEETH * f1e, 1.5 * f1p + 8.0),
        "Vib_EnvMod": envelope_modulation(x),
    }
