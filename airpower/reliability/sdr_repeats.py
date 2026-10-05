"""A8 on real data: repeated serials and repeat defects in FAA Service Difficulty Reports.

There is no ground truth here (nobody labels a unit 'rogue' in the SDR system), so this reports
what the same rules find in real reports, as evidence that the signals exist and the rules run
on real-world identifiers:
* repeated serials: the same part number + serial in more than one report; reported on more than
  one aircraft means the unit was repaired, refitted elsewhere and reported again, the classic
  rogue-unit pattern;
* repeat defects: the same aircraft (registration) and JASC code again within REPEAT_DAYS.
Reports are filed only for significant defects, so both counts are lower bounds of what an
operator's own records would show.

    python -m airpower.reliability.sdr_repeats      -> outputs/nlp/sdr_repeats.json
"""
import json
import re
import sys

import pandas as pd

from airpower.config import OUTPUTS, RAW
from airpower.ingest.resolve import key

YEARS = range(2020, 2026)
REPEAT_DAYS = 15
JUNK = re.compile(r"^(UNK\w*|NONE|NA|NIL|NR|NS|NOT\w*|NOSN|NOSERIAL\w*|N\w?A|SEE\w*|VARIOUS|MULTIPLE|ASREQ\w*|UNAVAIL\w*"
                  r"|\d{1,2}|0+|X+|9+)$")
PLACEHOLDER_AIRCRAFT = 5      # a real unit is not fitted to five or more aircraft within a few years


def load():
    cols = ["DifficultyDate", "RegistryNNumber", "JASCCode", "PartName", "PartNumber", "PartSerialNumber", "PartCondition",
            "AircraftMake", "AircraftModel"]
    d = pd.concat([pd.read_csv(RAW / "faa_sdrs" / f"SDR-{y}.csv", dtype=str, usecols=cols, encoding_errors="replace")
                   for y in YEARS], ignore_index=True)
    d["date"] = pd.to_datetime(d["DifficultyDate"], format="%m/%d/%Y", errors="coerce")
    d["pn"] = d["PartNumber"].map(lambda x: key(x) if isinstance(x, str) else "")
    d["sn"] = d["PartSerialNumber"].map(lambda x: key(x) if isinstance(x, str) else "")
    d["reg"] = d["RegistryNNumber"].map(lambda x: key(x) if isinstance(x, str) else "")
    return d


def main(argv=None):
    d = load()
    ok = (d["pn"].str.len() >= 3) & (d["sn"].str.len() >= 3) & ~d["sn"].str.match(JUNK) & ~d["pn"].str.match(JUNK)
    s = d[ok]
    g = s.groupby(["pn", "sn"]).agg(reports=("date", "size"), aircraft=("reg", "nunique"), first=("date", "min"),
                                    last=("date", "max"), part=("PartName", "first"),
                                    conditions=("PartCondition", lambda x: ",".join(sorted(set(x.dropna()))[:4]))).reset_index()
    placeholder = g["aircraft"] >= PLACEHOLDER_AIRCRAFT
    g = g[~placeholder]
    rep = g[g["reports"] >= 2]
    moved = rep[rep["aircraft"] >= 2]
    # repeat defects on one aircraft and JASC code
    r = d[(d["reg"].str.len() >= 3) & d["date"].notna()].sort_values(["reg", "JASCCode", "date"])
    gap = r.groupby(["reg", "JASCCode"])["date"].diff().dt.days
    # same-day reports are one inspection finding several defects, not a recurrence
    repeat_share = float(((gap >= 1) & (gap <= REPEAT_DAYS)).mean())
    same_day_share = float((gap == 0).mean())
    top_moved = moved.sort_values(["aircraft", "reports"], ascending=False).head(15)
    res = dict(reports=int(len(d)), reports_with_usable_serial=int(ok.sum()),
               placeholder_serials_excluded=int(placeholder.sum()),
               serials=int(len(g)), serials_reported_twice_or_more=int(len(rep)),
               serials_reported_on_two_or_more_aircraft=int(len(moved)),
               repeat_defect_share_1_to_15_days=repeat_share, same_day_multiple_report_share=same_day_share,
               examples_moved_serials=[dict(part=x.part, pn=x.pn, reports=int(x.reports), aircraft=int(x.aircraft),
                                            first=str(x.first.date()), last=str(x.last.date()), conditions=x.conditions)
                                       for x in top_moved.itertuples()])
    (OUTPUTS / "nlp").mkdir(parents=True, exist_ok=True)
    (OUTPUTS / "nlp" / "sdr_repeats.json").write_text(json.dumps(res, indent=1))
    print(json.dumps({k: v for k, v in res.items() if k != "examples_moved_serials"}, indent=1))
    print(top_moved[["part", "reports", "aircraft", "first", "last", "conditions"]].head(10).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
