"""Defect and rectification text, as a maintainer would write it in the servicing record.

The simulator stores the clean text in the truth tables. The records export then applies the
habits of real logbook entries (abbreviations, upper case, typos) so the NLP and integration
layers face the same problem they would on real data (cf. MaintNet's logbook corpora).
"""
import re

SYMPTOMS = {
    "ENG": ["engine EGT high during climb", "engine low power on take-off run", "engine N2 fluctuation in cruise",
            "engine performance below limits on ground run", "engine vibration high at max power",
            "engine slow to accelerate", "engine EGT margin below limit on trend check"],
    "FCU": ["fuel control unit RPM hunting at idle", "engine overfuel on throttle slam", "fuel flow fluctuation in cruise"],
    "FPMP": ["fuel pressure low caution", "main fuel pump pressure fluctuation"],
    "FBP": ["boost pump fail caption", "fuel boost pressure low in inverted flight"],
    "JFS": ["jet fuel starter failed to start", "JFS overtemperature on start", "JFS disengage caption"],
    "GEN": ["generator off-line caption in flight", "generator voltage fluctuation", "AC bus fail caption"],
    "SGEN": ["starter-generator off-line caption", "starter-generator slow engine start"],
    "TRU": ["DC bus voltage low", "TRU fail caption"],
    "BAT": ["battery voltage low on start", "battery capacity check below limit", "battery overheat caution"],
    "HYDP": ["hydraulic pressure low caution", "hydraulic system pressure fluctuation", "hydraulic pump noisy on ground run"],
    "ACCU": ["accumulator pre-charge low", "hydraulic accumulator leaking"],
    "MLGA": ["main gear slow to retract", "landing gear unsafe indication", "main gear actuator leak"],
    "NWS": ["nose wheel steering inoperative", "nose wheel shimmy on landing roll"],
    "BRK": ["brake wear pin below limit", "brake judder on landing", "brake pedal spongy"],
    "TYRE": ["tyre worn to limit", "tyre cut found on walk-round", "tyre pressure loss"],
    "FCA": ["flight control servo-actuator leak", "flight control fail caption", "stick force abnormal in roll"],
    "ADC": ["air data computer fail caption", "airspeed disagree", "altitude indication erratic"],
    "INS": ["INS alignment failed", "INS drift excessive", "navigation fail caption"],
    "NAV": ["GPS/INS alignment failed", "navigation position error excessive"],
    "RDR": ["radar no transmit", "radar fail caption in flight", "radar picture lost intermittently"],
    "MC": ["mission computer reboot in flight", "mission computer fail caption"],
    "MFD": ["MFD blank", "display flicker on MFD", "MFD no response to bezel keys"],
    "VUHF": ["V/UHF radio no transmit", "radio reception weak and noisy", "radio fail on preset channels"],
    "IFF": ["IFF fail caption", "IFF mode 3 not replying"],
    "RALT": ["radar altimeter erratic below 500 ft", "radar altimeter fail flag"],
    "ECS": ["cockpit temperature high", "ECS overheat caption", "air cycle machine noisy"],
    "OBOG": ["oxygen low pressure caution", "OBOGS fail caption"],
    "CART": ["ejection seat cartridges due for life replacement"],
    "FIRE": ["engine fire warning spurious", "fire detection loop fail on test"],
    "MGB": ["main gearbox chip light", "main gearbox oil pressure fluctuation", "main gearbox vibration trend high on HUMS"],
    "IGB": ["intermediate gearbox chip light", "intermediate gearbox temperature high", "IGB vibration trend high on HUMS"],
    "TGB": ["tail gearbox chip light", "tail gearbox oil leak", "TGB vibration trend high on HUMS"],
    "MRB": ["main rotor blade erosion beyond limit", "main rotor blade debond found", "rotor track out of limits"],
    "MRD": ["main rotor damper leaking", "lead-lag damper ground resonance tendency", "1R vibration high"],
    "TRB": ["tail rotor blade erosion beyond limit", "tail rotor blade crack found"],
    "SWP": ["swashplate bearing play excessive", "swashplate bearing noise"],
    "HGRB": ["hanger bearing temperature strip discoloured", "tail drive shaft hanger bearing rough"],
    "SERVO": ["main rotor servo leak", "servo jam caption on test"],
    "AFCS": ["AFCS disengage in flight", "AFCS actuator fail on preflight test"],
    "STRUT": ["shock strut leaking", "shock strut extension low"],
}

SNAGS = [
    (33, "cockpit flood light inoperative"), (33, "navigation light u/s"), (30, "pitot heat caution on ground"),
    (23, "intercom crackle"), (56, "canopy seal leak"), (25, "harness buckle stiff"),
    (31, "caution panel lamp u/s"), (21, "demist fan noisy"), (52, "access panel fastener missing"),
    (29, "minor hydraulic seepage at union"), (28, "fuel quantity indication erratic"), (32, "taxi light u/s"),
    (24, "circuit breaker tripped, reset ok"), (35, "oxygen mask regulator stiff"), (34, "compass deviation card missing"),
    (36, "bleed air duct clamp loose"), (79, "oil level low, replenished"), (12, "servicing panel latch broken"),
]

RECT = {
    "replace": "carried out troubleshooting, replaced {name} (P/N {pn}) S/N {old} with S/N {new}; ops check satisfactory",
    "replace_nff": "replaced {name} S/N {old} with S/N {new}; ground run satisfactory, removed unit sent for test",
    "inspection": "found during {insp}: replaced {name} S/N {old} with S/N {new}",
    "life": "{name} S/N {old} replaced on life expiry with S/N {new}",
    "preventive": "{name} S/N {old} replaced preventively at {insp} with S/N {new}",
    "predictive": "{name} S/N {old} replaced on condition (HUMS trend / planned) with S/N {new}",
    "snag": "rectified, ops check satisfactory",
}

ABBREV = [
    (r"\bengine\b", "eng"), (r"\bhydraulic\b", "hyd"), (r"\bpressure\b", "press"), (r"\btemperature\b", "temp"),
    (r"\breplaced\b", "repl"), (r"\bsatisfactory\b", "sat"), (r"\binoperative\b", "inop"), (r"\bcheck\b", "chk"),
    (r"\bcarried out\b", "c/o"), (r"\btroubleshooting\b", "t/s"), (r"\bcaution\b", "caut"), (r"\bfluctuation\b", "fluct"),
    (r"\bmain\b", "mn"), (r"\bgearbox\b", "gbx"), (r"\bunserviceable\b", "u/s"), (r"\bindication\b", "ind"),
    (r"\bvibration\b", "vib"), (r"\bground run\b", "g/run"), (r"\bwith\b", "w/"), (r"\bfound\b", "fnd"),
]


def symptom(pos, rng):
    opts = SYMPTOMS.get(pos)
    return str(rng.choice(opts)) if opts else "system fault reported"


def noisy(text, rng, abbrev_p=0.6, typo_p=0.04, upper_p=0.5):
    """Logbook style: abbreviations, sometimes all capitals, occasional typos."""
    s = text
    for pat, rep in ABBREV:
        if rng.random() < abbrev_p:
            s = re.sub(pat, rep, s, flags=re.IGNORECASE)
    words = s.split(" ")
    for i, w in enumerate(words):
        if len(w) > 4 and w.isalpha() and rng.random() < typo_p:
            j = int(rng.integers(1, len(w) - 1))
            k = int(rng.integers(3))
            words[i] = w[:j] + w[j + 1:] if k == 0 else (w[:j] + w[j + 1] + w[j] + w[j + 2:] if k == 1 else w[:j] + w[j] + w[j:])
    s = " ".join(words)
    return s.upper() if rng.random() < upper_p else s
