"""Rigol DHO924S SCPI driver (spec §16.2). PyVISA + pyvisa-py backend, no vendor VISA install.

Read/measure paths are SAFE and fully working (verified live). The AWG source syntax is NOT assumed:
it is probed at bind time against ``:SYSTem:ERRor?`` (spec §15 caveat), never hard-coded.
"""
from __future__ import annotations

import struct
import time
from typing import Any

import numpy as np


class DHO924S:
    def __init__(self, resource: str, timeout_ms: int = 5000):
        import pyvisa
        self._rm = pyvisa.ResourceManager("@py")
        self.resource = resource
        self.inst = self._rm.open_resource(resource)
        self.inst.timeout = timeout_ms
        self.inst.read_termination = "\n"
        self.inst.write_termination = "\n"
        self.idn = self.inst.query("*IDN?").strip()

    # -- basic -----------------------------------------------------------------------
    def query(self, q: str) -> str:
        return self.inst.query(q).strip()

    def write(self, cmd: str) -> None:
        self.inst.write(cmd)

    def system_error(self) -> str:
        try:
            return self.inst.query(":SYSTem:ERRor?").strip()
        except Exception as e:
            return f"err-query-failed:{e}"

    def close(self) -> None:
        try:
            self.inst.close()
        except Exception:
            pass

    # -- capabilities (live-read) ----------------------------------------------------
    def capabilities(self) -> dict:
        caps: dict[str, Any] = {"idn": self.idn}
        parts = self.idn.split(",")
        if len(parts) >= 4:
            caps.update({"vendor": parts[0], "model": parts[1], "serial": parts[2],
                         "firmware": parts[3]})
        for key, q in (("impedance", ":CHANnel1:IMPedance?"), ("srate", ":ACQuire:SRATe?"),
                       ("mdepth", ":ACQuire:MDEPth?")):
            try:
                caps[key] = self.query(q)
            except Exception:
                caps[key] = None
        caps["channels"] = 4
        caps["rated_max_input_v"] = 400   # DHO900 1 MΩ CAT I; also stored in rig_config
        return caps

    # -- measure (SAFE) --------------------------------------------------------------
    def measure(self, item: str, ch: int = 1) -> float:
        try:
            return float(self.query(f":MEASure:ITEM? {item},CHANnel{ch}"))
        except Exception:
            return float("nan")

    def measure_set(self, ch: int = 1) -> dict:
        try:
            self.write(f":CHANnel{ch}:DISPlay ON")
            # Measurements need a LIVE acquisition. If the scope is left in single/normal sweep
            # with no trigger (e.g. after arm_single) it never acquires and every item reads the
            # 9.9E37 "no measurement" sentinel. Force AUTO sweep + RUN so we read real values.
            self.write(":TRIGger:SWEep AUTO")
            self.write(":RUN")
            time.sleep(0.5)   # let the acquisition stabilize after any scale/timebase change,
                              # else VMAX/etc. read the 9.9E37 "no measurement" sentinel
        except Exception:
            pass
        items = {"vpp": "VPP", "vmax": "VMAX", "vmin": "VMIN", "pwidth": "PWIDth",
                 "rise": "RTIMe", "freq": "FREQuency"}
        out = {}
        for k, v in items.items():
            val = self.measure(v, ch)
            # DHO returns ~9.9E37 as a "no measurement" sentinel; normalize to None
            out[k] = None if (val != val or abs(val) > 1e30) else round(val, 6)
        return out

    # -- waveform (SAFE) -------------------------------------------------------------
    def read_waveform(self, ch: int = 1, frozen: bool = False) -> tuple[np.ndarray, dict]:
        # Ensure the channel is displayed, let it sweep, then freeze the buffer so DATA? returns
        # the real on-screen record (DHO returns an empty record while free-running/untriggered).
        # frozen=True: the record is ALREADY captured/stopped (e.g. after a single-shot trigger);
        # read it as-is WITHOUT re-running (which would discard the trigger-aligned acquisition).
        self.write(f":CHANnel{ch}:DISPlay ON")
        if not frozen:
            try:
                self.write(":TRIGger:SWEep AUTO")   # acquire without needing a trigger
                self.write(":RUN")
                time.sleep(0.2)
                self.write(":STOP")
                time.sleep(0.05)
            except Exception:
                pass
        self.write(f":WAVeform:SOURce CHANnel{ch}")
        self.write(":WAVeform:MODE NORMal")
        self.write(":WAVeform:FORMat BYTE")
        pre = self.query(":WAVeform:PREamble?").split(",")
        # preamble: format,type,points,count,xincr,xorig,xref,yincr,yorig,yref
        p = [float(x) for x in pre]
        xincr, xorig, xref = p[4], p[5], p[6]
        yincr, yorig, yref = p[7], p[8], p[9]
        raw = self.inst.query_binary_values(":WAVeform:DATA?", datatype="B", container=bytes)
        data = np.frombuffer(raw, dtype=np.uint8).astype(float)
        if data.size == 0:
            data = np.zeros(2)
        volts = (data - yref - yorig) * yincr
        preamble = {"points": int(p[2]), "xincr": xincr, "xorig": xorig, "yincr": yincr,
                    "yorig": yorig, "yref": yref, "dt_s": xincr, "t0_s": xorig}
        return volts, preamble

    def screenshot_png(self) -> bytes:
        return self.inst.query_binary_values(":DISPlay:DATA? PNG", datatype="B", container=bytes)

    # -- acquisition config (SAFE) ---------------------------------------------------
    def configure_acquisition(self, timebase_s: float | None = None,
                              trig_level_v: float | None = None,
                              trig_source: str | None = None) -> dict:
        applied = {}
        if timebase_s is not None:
            self.write(f":TIMebase:SCALe {timebase_s}")
            applied["timebase_s"] = timebase_s
        if trig_source is not None:
            self.write(f":TRIGger:EDGE:SOURce {trig_source}")
            applied["trig_source"] = trig_source
        if trig_level_v is not None:
            self.write(f":TRIGger:EDGE:LEVel {trig_level_v}")
            applied["trig_level_v"] = trig_level_v
        return applied

    # -- single-shot triggered acquisition (SAFE; for timing discovery) --------------
    def arm_single(self, trig_source: str | None = None, trig_level_v: float | None = None,
                   slope: str = "POSitive") -> dict:
        """Arm ONE edge-triggered acquisition, then return immediately. Fire the trigger event
        (e.g. reboot the target) AFTER this; poll trigger_status() until 'STOP', then read_waveform(
        ch, frozen=True) to get the trigger-aligned record (t=0 at the trigger)."""
        if trig_source is not None:
            self.write(f":TRIGger:EDGE:SOURce {trig_source}")
        if trig_level_v is not None:
            self.write(f":TRIGger:EDGE:LEVel {trig_level_v}")
        self.write(":TRIGger:MODE EDGE")
        self.write(f":TRIGger:EDGE:SLOPe {slope}")
        self.write(":SINGle")
        return {"armed": True, "trig_source": trig_source, "trig_level_v": trig_level_v,
                "slope": slope}

    def trigger_status(self) -> str:
        """TD | WAIT | RUN | STOP | AUTO."""
        try:
            return self.query(":TRIGger:STATus?")
        except Exception as e:
            return f"err:{e}"

    def configure_channel(self, ch: int, scale_v_per_div: float | None = None,
                          offset_v: float | None = None, coupling: str | None = None,
                          probe_ratio: float | None = None) -> dict:
        applied = {}
        if probe_ratio is not None:
            self.write(f":CHANnel{ch}:PROBe {probe_ratio}")
            applied["probe_ratio"] = probe_ratio
        if scale_v_per_div is not None:
            self.write(f":CHANnel{ch}:SCALe {scale_v_per_div}")
            applied["scale_v_per_div"] = scale_v_per_div
        if offset_v is not None:
            self.write(f":CHANnel{ch}:OFFSet {offset_v}")
            applied["offset_v"] = offset_v
        if coupling is not None:
            self.write(f":CHANnel{ch}:COUPling {coupling}")
            applied["coupling"] = coupling
        return applied

    # -- source syntax probe (spec §15/§16.3) ----------------------------------------
    def probe_source_syntax(self) -> dict:
        """Probe candidate AWG SCPI roots against :SYSTem:ERRor?; never hard-code.

        Returns the working root (if any). This is READ-only probing (queries), so it is SAFE —
        it does not enable any output.
        """
        # clear error queue
        self.system_error()
        candidates = [":SOURce1:OUTPut", ":SOUR1:OUTP", ":GEN:OUTP", ":AWG1:OUTP",
                      ":SOURce1:FUNCtion", ":SOUR1:FUNC"]
        working = None
        results = {}
        # Fail-fast: unrecognized queries never answer on the DHO924S (they just flash a
        # "Remote cmd error"), so use a short timeout instead of blocking the full 5 s each.
        saved_to = self.inst.timeout
        self.inst.timeout = 400
        try:
            for root in candidates:
                try:
                    _ = self.inst.query(f"{root}?")
                    err = self.system_error()
                    ok = "0," in err.split(",")[0] or err.startswith("0")
                    results[root] = err
                    if ok and working is None:
                        working = root
                except Exception as e:
                    results[root] = f"exc:{type(e).__name__}"
        finally:
            self.inst.timeout = saved_to
        return {"working_root": working, "probed": results,
                "note": "AWG unused in this campaign; probe is read-only/SAFE (fail-fast)"}
