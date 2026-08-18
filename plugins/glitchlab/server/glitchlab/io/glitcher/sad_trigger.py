"""Optional SAD (Sum-of-Absolute-Differences) triggering for the ChipWhisperer-Husky.

Joe Grand / Leonard's "Failure is Not an Option" identifies SAD triggering as THE key improvement
over fixed-time-offset triggering: the boot timing varies chip-to-chip and shot-to-shot, so a fixed
ext_offset from reset lands at a different point in the boot each time; SAD instead triggers on a
POWER-CONSUMPTION FINGERPRINT, hitting the same boot instant every shot regardless of jitter.

This is fully OPTIONAL and self-detecting. It requires a target activity signal wired into the
Husky's ADC (the POS measure SMA, short-cap on NEG). If that connection is absent, `probe()` sees no
boot signature on the ADC and SAD stays disabled — the adapter then uses the existing tio4/reset
trigger exactly as before. Nothing here can break the proven fixed-offset path: every entry point is
guarded and returns False on any error, leaving the caller to fall back.

Husky SAD API (verified against chipwhisperer 6.0.0 ChipWhispererSAD.HuskySAD):
    scope.trigger.module = 'SAD'
    scope.SAD.reference  = <numpy waveform>   # first sad_reference_length samples are used
    scope.SAD.threshold  = <int>              # SAD score below threshold => trigger
    scope.SAD.sad_reference_length            # hardware reference length (samples)

On-hardware calibration is still required (reference window + threshold) once the POS tap is wired;
this module captures a sensible default reference and a mid-range threshold, and logs everything so
the values can be tuned live. Until calibrated it errs toward staying DISABLED rather than mis-firing.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

import numpy as np


class SadTrigger:
    def __init__(self, scope, *, trigger_line: str = "tio4", logger=None) -> None:
        self.s = scope
        self.trigger_line = trigger_line
        self.log = logger or (lambda *a, **k: None)
        self.available = False        # POS tap present + ADC sees a real boot signature
        self.active = False           # SAD is configured and armed as the trigger source
        self.reference: Optional[np.ndarray] = None
        self.threshold: Optional[int] = None
        self.ref_offset = 0           # ADC samples from reset before the reference window
        self.detail: dict = {}

    # -- low-level ADC capture (basic tio4 trigger; does NOT glitch) --------------------------------
    def _capture_adc(self, reboot_fn: Callable[[], None], samples: int, offset: int):
        """Arm the ADC on the tio4/reset edge, reboot, return the captured power trace (or None)."""
        s = self.s
        s.adc.samples = int(samples)
        try:
            s.adc.offset = int(offset)
        except Exception:
            pass
        s.trigger.module = "basic"
        s.trigger.triggers = self.trigger_line
        s.arm()
        time.sleep(0.02)
        reboot_fn()
        ret = s.capture()             # True == capture timeout (no trigger)
        if ret:
            return None
        try:
            return np.asarray(s.get_last_trace(), dtype=float)
        except Exception:
            return None

    # -- 1) is the POS tap connected? (auto-detect) ------------------------------------------------
    def probe(self, reboot_fn: Callable[[], None], *, samples: int = 5000) -> bool:
        """Capture one boot power trace on the ADC. If it shows real activity (not flat/noise), the
        target-activity signal is present and SAD is usable. Any failure => not available."""
        try:
            self.s.trigger.module = "basic"       # need a working edge trigger to grab the reference
            self.s.trigger.triggers = self.trigger_line
            tr = self._capture_adc(reboot_fn, samples=samples, offset=0)
            if tr is None or tr.size < 64:
                self.detail = {"reason": "no ADC capture (no POS tap / no trigger)"}
                self.available = False
                return False
            span = float(np.percentile(tr, 99) - np.percentile(tr, 1))
            std = float(np.std(tr))
            # A live boot rail has clear structure; a disconnected AC-coupled input is ~flat noise.
            self.available = span > 0.02 and std > 0.005
            self.detail = {"adc_span": round(span, 4), "adc_std": round(std, 4),
                           "available": self.available, "samples": int(tr.size)}
            self.log("sad.probe", self.detail)
            return self.available
        except Exception as e:  # noqa
            self.detail = {"reason": f"probe exception: {e!r}"}
            self.available = False
            return False

    # -- verify SAD actually fires (not just that it's configured) ---------------------------------
    def _test_trigger(self, reboot_fn: Callable[[], None], n: int = 3) -> float:
        """Arm with the current SAD config, reboot, and count how many boots actually trigger.
        Returns the trigger rate 0..1. capture()==True means timeout (no match => no trigger)."""
        hits = 0
        for _ in range(n):
            try:
                self.s.arm()
                time.sleep(0.02)
                reboot_fn()
                if not self.s.capture():
                    hits += 1
            except Exception:
                pass
        return hits / max(1, n)

    # -- 2) configure SAD from a captured reference, AUTO-TUNE threshold, VERIFY it triggers --------
    def configure(self, reboot_fn: Callable[[], None], *, ref_offset: int = 0,
                  threshold: Optional[int] = None) -> bool:
        """Capture a reference boot trace, load the fingerprint, then auto-tune the threshold and
        VERIFY SAD reliably triggers. Only enables SAD if it fires >=75% of test boots; otherwise
        disables it and returns False so the caller uses the proven tio4/ext_offset trigger. This is
        what makes 'auto' safe: SAD is used only if it actually works, never if it just times out."""
        if not self.available:
            return False
        try:
            sad = self.s.SAD
            reflen = int(sad.sad_reference_length)
            need = ref_offset + reflen + 256
            tr = self._capture_adc(reboot_fn, samples=need, offset=0)
            if tr is None or tr.size < ref_offset + reflen:
                self.log("sad.configure", {"fail": "reference capture too short"})
                self.disable(); return False
            ref = np.asarray(tr[ref_offset: ref_offset + reflen], dtype=np.float64)
            sad.reference = ref
            self.reference = ref
            self.ref_offset = int(ref_offset)
            max_thr = 2 ** (sad._sad_counter_width - 1)
            self.s.trigger.module = "SAD"
            # Sweep candidate thresholds low->high (lower = stricter match = less spurious); pick the
            # LOWEST that triggers reliably. SAD triggers when score < threshold.
            candidates = ([int(threshold)] if threshold
                          else [max(1, int(max_thr * f)) for f in (0.35, 0.5, 0.65, 0.8)])
            best = None
            for thr in candidates:
                thr = max(1, min(int(thr), max_thr))
                sad.threshold = thr
                self.s.trigger.module = "SAD"
                rate = self._test_trigger(reboot_fn, n=3)
                self.log("sad.tune", {"threshold": thr, "trigger_rate": rate})
                self.detail.setdefault("tune", []).append({"thr": thr, "rate": round(rate, 2)})
                if rate >= 0.75:
                    best = thr
                    break
            if best is None:
                self.detail["result"] = "no threshold triggered reliably -> fall back to tio4"
                self.disable(); return False
            sad.threshold = best
            self.threshold = best
            self.s.trigger.module = "SAD"
            self.active = True
            self.detail.update({"reflen": reflen, "threshold": best, "max_threshold": max_thr,
                                "ref_offset": self.ref_offset, "result": "SAD active"})
            self.log("sad.configure", {"ok": True, **self.detail})
            return True
        except Exception as e:  # noqa
            self.log("sad.configure", {"fail": repr(e)})
            self.disable()
            return False

    # -- restore the normal edge trigger ------------------------------------------------------------
    def disable(self) -> None:
        self.active = False
        try:
            self.s.trigger.module = "basic"
            self.s.trigger.triggers = self.trigger_line
        except Exception:
            pass

    def status(self) -> dict:
        return {"available": self.available, "active": self.active,
                "threshold": self.threshold, "ref_offset": self.ref_offset, **self.detail}
