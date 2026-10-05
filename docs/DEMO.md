# AirPower: 10-minute demo script

For a presenter who has not seen the dashboard before. Start the API and dashboard
(`.venv/Scripts/python -m server.app --demo-users`, then open http://127.0.0.1:8000) and sign in. The
demo accounts are in `deploy/demo_users.yaml` (user names `commander`, `engineer`, `logistics`,
`analyst`, `admin`; demos only). Sign in as the **commander** for steps 1-6, as the **engineer** to
accept a planned job in step 4, and as the **admin** at the end for the audit log. Every view opens with
a short "how to read this" note; close it with *Got it* (reopen with *? How to read this view*).

The data is a simulated fleet of 48 aircraft (two generic types, two bases, a depot) whose health
signals are real NASA and university datasets, with its records split across four deliberately
inconsistent systems. Say so at the start.

## 1. The problem in one screen (Readiness, 2 minutes)

* Top row: serviceable aircraft per unit, and why the others are down (maintenance, spares,
  scheduled servicing).
* *Aircraft waiting for parts*: the parts that keep aircraft on the ground today.
* *Forecast: next 30 days*: the fleet twin's forecast from today's records, with its band. Point
  out that it is a forecast of many possible futures, not a single line.
* *Components at risk (HUMS)*: predictions from the health models, highest consequence first.
* Optional: change the date (top right) to show the fleet on any morning in the records.

## 2. Where are the problems? (Health map, 1 minute)

* One row per aircraft, one column per system: red act now, amber plan it.
* Hover a red cell for the reason; click the row of a helicopter with a red *Spares* or *Rotor
  blades* cell (for example H2009).

## 3. One aircraft, its twin and what to do (Aircraft page, 2 minutes)

* *Recommended actions*: each says what to do, why (the model's numbers and drivers) and which
  model or record raised it. Example on H2009: a tail-rotor blade at 99 % of its life with no
  spare in the fleet, so the spares model says *expedite*, and the planner shows the aircraft
  grounded until the spare arrives.
* *Predicted remaining life*: dot = prediction, bar = 90 % interval. Plan on the left end.
* Life-limited parts, inspections due, the HUMS trend, the defect history by ATA chapter.
* *Aircraft report (print / PDF)*: the same page as a printable report.

## 4. Plan and spares (Planner and Spares, 2 minutes)

* Planner: the optimiser's 90-day plan against the due-date rule (fewer aircraft-days down, fewer
  separate inductions, removals bundled into inspections). Accept or move a job in *Next 14 days*;
  the decision is recorded with the user and time.
* Spares: the curve shows availability for each level of spares money. The best allocation of
  today's budget buys more availability for less money; the simulator check below the chart
  confirms it in fresh simulated fleets.

## 5. Test a decision before taking it (What if, 2 minutes)

* Click *Exercise next month*: the twin runs 20 futures of 90 days with and without the exercise
  and shows the dip in serviceable aircraft and the flying task met. Takes about a minute.
* Click *Predictive policy* or *Recommended stock*: the difference is shown with its 95 %
  interval, measured on the same random futures.

## 6. Does it work? (Evidence, 1 minute)

* The table compares maintenance policies over 30 paired simulated years each: reactive (today),
  preventive, and predictive (AirPower), with and without the recommended stock.
* *Value of prediction*: the gain when the models' errors are doubled or tripled on purpose, and
  with no HUMS at all. This answers "what if your models are worse than you think?".
* *Can the twin forecast?*: forecasts made from the records against what the simulated fleet did
  next.

Close on *Data integration* (sign in as the **analyst**) if asked how the four systems are joined:
every inconsistency found and what was done about it.

## 7. Who can do what (1 minute, optional)

Sign out and sign in as the **engineer**: the Planner shows *Accept* and *Move* on the next 14 days'
jobs; the commander sees "engineering officers decide". As the **admin**: *Administration* lists the
users and the audit log (sign-ins, the decision just taken, report exports, refused requests);
*Verify the chain* shows nothing has been removed or changed. The administrator cannot run what-ifs,
export reports or decide plans: separation of duties.
