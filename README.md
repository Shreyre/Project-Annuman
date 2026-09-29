# Anumaan

**The register says it's on the shelf. The care says otherwise.**

Anumaan ("inference") finds medicine stock-outs at Primary Health Centres that the stock register hides. It reads them from the care that is already being recorded. When a PHC runs out of a medicine, patients are still diagnosed, but full courses stop, courses get cut short, guideline substitutes appear and slips say "not available". Anumaan decodes those fingerprints against India's treatment guidelines to infer what is really on the shelf. It then plans what to move, what to escalate and what the next two weeks will need.

Built for **Build with AI: Code for Communities, Second Edition**, Track 03 (Smart Health & Supply Chain Resilience).

## How it works

1. **Compile the guidelines.** Gemini reads ICMR Standard Treatment Workflows, NHSRC guidelines and the Essential Medicines List. It writes a cited *care-to-resource grammar*: for each diagnosis, the medicine course it should consume, the permitted substitutes, and the page it came from. A second Gemini pass drops any rule the PDF does not support. See `grammar/compile_crg.py`.
2. **Infer the shelf.** For every PHC and medicine, a Bayesian filter compares the courses the diagnoses called for with what was dispensed: full, cut short, substituted, not available, or silently missing. See `anumaan/filter.py`.
   - The register is one more sensor, weighted by how well that PHC's register tracked reality in its first month.
   - A day with no slips entered on any medicine is a data-entry gap, not a stock-out.
   - A pharmacist's shelf check, by button or by voice, replaces the day's belief.
   - A **shadow stock** warns before the shelf empties even where staff never ration. It is the register's opening balance plus deliveries, minus what the dispensing slips took out. Registers post issues late or never; the slips are the issues. It is reset to zero whenever the care shows the shelf empty. Under 8 days of use left raises an alarm.
3. **Localise and act.** "Where it broke" is read from the warehouse ledger (DVDMS records each district warehouse's weekly indent to the state and what arrived). A warehouse whose indents the state has stopped filling is starved. One starved warehouse is a warehouse failure, two or more in a state is a state procurement failure, and two or more in each of two states is national. If supply is still flowing, a state-wide jump in diagnoses marks a demand surge; otherwise the problem is at the PHC. Then Google OR-Tools plans same-state transfers in treatment courses from PHCs that have been calm all week, sized on their shadow stock, and escalates what moving stock cannot fix. See `anumaan/triage.py` and `anumaan/planner.py`.
4. **See beds, staff and demand.**
   - **Beds** come from the admission-discharge feed, with stays that were never closed auto-closed after 5 days.
   - **Staff** are checked against the care record, by role only: a doctor marked present but with no prescriptions on a 40-patient day gets flagged.
   - **Demand** for the next 14 days is forecast from diagnoses through the grammar, not from dispensing that stock-outs have already cut.

   See `anumaan/care.py` and `anumaan/forecast.py`.
5. **Federate.** Each state keeps its own diagnoses, slips and warehouse ledger. Only counts per district and medicine cross the state line, plus whether the state has stopped supplying that district's warehouse. They pass a gate that rejects anything raw and any group under five PHCs. The national view flags a national shortage when starved warehouses show up in two or more states, and hands back shared starting parameters for new states. See `anumaan/federation.py`.

## Results so far: synthetic data only

There is no public patient-level dispensing data in India, so everything below comes from a simulator (`anumaan/sim.py`). It mimics the shape of the real feeds and injects failures as hidden ground truth. It is deliberately messy: part-filled indents, upstream failures that still trickle supply, 30-90 day warehouse buffers, a monsoon surge, data-entry gaps, registers that drift, substitutes that fail too, and a warehouse ledger posted 0-10 days late that loses 5% of receipts.

Thresholds were tuned on seeds 0-4. Every number below is from **held-out seeds 5-9**. Each module's CLI reproduces its numbers.

**Detecting hidden stock-outs** (`python -m anumaan.evaluate --seeds 5-9`)

| Behaviour of frontline staff | Stock-outs of 4+ days caught | Warned before the shelf emptied | False alarms per medicine per PHC per year | Register tuned to the same false-alarm budget |
|---|---|---|---|---|
| Ration when stock runs low (default) | 98-100% | 92-98% (median 5-7 days ahead) | 0.02-0.08 | 9-35% caught |
| Different behaviour model, no "not available" slips (`--behaviour alt`) | 98-100% | 65-74% (3-4 days ahead) | 0.03-0.10 | 21-59% caught |
| Never ration (`--ration 0`) | 97-99% | 54-61% (2-3 days ahead) | 0.03-0.04 | 5-41% caught |

Where staff do not ration, the warning comes from the shadow stock. The care record alone warns for only 31-37% (alt) and 3-8% (never ration) of stock-outs, and catches 66-70% of all stock-outs instead of 90-93% when staff never ration. Without diagnoses (the same filter and shadow stock run on each medicine's dispensing history) Anumaan catches as many stock-outs, but with 20x or more false alarms.

**Where it broke** (same runs, labelled 7 days after the alarm)

| Behaviour of frontline staff | Stock-outs labelled right | Right on average per cause | Always guessing the commonest cause |
|---|---|---|---|
| Default | 80-92% | 72-91% | 37-46% |
| Alt | 82-89% | 74-88% | 42-56% |
| Never ration | 82-93% | 73-88% | 44-52% |

Before the warehouse ledger, clustering alarms up the supply tree scored 36-56% on default: no better than guessing.

**The other modules**

| Module | Held-out result | What we are not claiming |
|---|---|---|
| Beds (`python -m anumaan.care`) | Count off by 0.30-0.36 beds on average, against 2.0-2.4 for plain admitted-minus-discharged | A quarter to a third of "every bed taken" days are artefacts of unrecorded discharges |
| Staff | "Marked present, no work on a busy day" flags are right 96-97% of the time and catch 69-76% of false attendance | A simple ratio rule does nearly as well; staff nurses are too rarely busy to check (recall about 0) |
| Forecast (`python -m anumaan.forecast`) | 14-day demand error (WAPE) 0.068-0.076 from diagnoses, against 0.119-0.128 for consumption with stock-out days filled in | Part of the edge is built in, because the simulator defines demand as diagnoses times the grammar. After stock-outs the lead is only 1.1-2.0x. Neither method sees the monsoon coming |
| Redistribution (`python -m anumaan.planner`) | 73-90% of planned courses go to PHCs that are truly short (a register-driven plan manages 47-61%); 86-91% come from PHCs with true surplus; it meets 46-53% of the need (register plan 30-38%). 64-100% of escalations are true state or national failures | Plans are never applied to the simulated world, so each week re-plans the same shortages. Travel times are straight-line estimates |
| Federation (`python -m anumaan.federation`) | About 5 KB of counts leave each state instead of about 125,000 raw records; the gate rejects doctored exports. The national-shortage flag caught **5 of 5** injected national failures with no flags on other medicines, ahead of 44 of the 54 PHC stock-outs they caused. Shared priors cut a 3-day-old state's false alarms from 0.07 to 0.04 per medicine per PHC per year (fewer in 8 of 10 cold states), the same as a 30-day warm-up | The flag goes up 26-35 days into a failure, after the first PHCs have run out. The priors gain is small in absolute terms: the care filter needs little calibration, and the gain comes through the shadow stock's days of use |

What we are **not** claiming overall:

- **The shadow stock is only as good as its first balance and the slips.** It starts from the register's opening balance, so an overstated opening delays the first warning until the care shows an empty shelf and resets it. A shelf count at go-live fixes that. A PHC that dispenses without writing slips would fool it.
- **"Where it broke" needs the warehouse ledger.** The simulator's ledger is cleaner than a real DVDMS feed: late and lossy, but otherwise exact. A state without one gets only "at this PHC" or "demand surge". The scorer counts a stock-out during an upstream failure as that failure even when its own warehouse still had stock; the ledger sees those failures because the indents stop being filled.
- **The simulator is kinder than reality.** Its shelves are stocked 97-99% of the time, while Indian PHC surveys report 72-75%.
- **Nothing has been validated on real data yet.** A pilot would check against real signals first, such as HMIS "discharged under 48 hours", state drug-availability dashboards and DVDMS warehouse records.

## Run it

```bash
pip install -e ".[app,gemini,dev]"                 # numpy, ortools, fastapi, uvicorn, google-genai, pytest
python -m pytest -q                               # 20 checks, incl. held-out end-to-end runs and the app
python -m anumaan.evaluate --seeds 5-9            # detection table; also --behaviour alt, --ration 0
python -m uvicorn app.main:app --port 8788        # demo at http://127.0.0.1:8788
```

The demo replays a synthetic network of 36 PHCs, 6 warehouses and 2 states over 200 days, on held-out seed 5. Scrub or play through the days.

- **Select a PHC and medicine** to see the register against the inferred shelf, the evidence, "where it broke", the pharmacist check (buttons in English and Odia, or a recorded answer), the 14-day forecast, and that PHC's beds and staff.
- **Below the grid:** today's transfers and escalations, and what the national view sees.
- **Tick "Show ground truth"** to compare with what the simulator hid.

## Google AI and deployment

| Piece | Where | Status |
|---|---|---|
| Guideline grammar, Gemini 3.7 Flash (PDF in, JSON schema out, verification pass) | `grammar/compile_crg.py` | Written; not run yet. The demo uses a hand-written seed grammar |
| Voice shelf check, Gemini 3.7 Flash (audio in, answer and cause out) | `anumaan/voice.py`, `/api/voice` | Wired end to end; says "not configured" until Vertex AI is set up |
| Forecasting on BigQuery AI.FORECAST with TimesFM 3.0 | `forecast.bigquery_sql` | SQL written, not executed; the demo uses the same model family locally |
| Routing with Google Maps Routes, redistribution with Google OR-Tools | `anumaan/planner.py` | OR-Tools runs; the Routes call is a stub, and travel times are straight-line estimates |
| Cloud Run deploy, budget alert | `Dockerfile`, `deploy/deploy.sh`, `deploy/budget.sh` | Syntax-checked, not run |

To go live:

1. Link a billing account to GCP project `anumaan-c4c`.
2. Run `gcloud auth application-default login`.
3. Run `bash deploy/budget.sh <billing-account> 2000`, then `bash deploy/deploy.sh`.
4. Compile the real grammar: `python grammar/compile_crg.py <ICMR STW PDFs> -o grammar/crg/compiled.json`.

## Layout

| Path | What |
|---|---|
| `anumaan/crg.py` | Loads the grammar; decodes slips into full / cut short / substitute / not available |
| `anumaan/filter.py` | Per PHC x medicine Bayesian filter, data-entry gaps, learned register trust, shelf checks, shadow stock |
| `anumaan/triage.py` | Where it broke, from the warehouse ledger's unfilled indents and state-wide demand |
| `anumaan/planner.py` | OR-Tools min-cost-flow transfers in treatment courses, donors sized on the shadow stock, escalations, DVDMS-style orders |
| `anumaan/forecast.py` | Diagnosis-driven demand forecast and the fair consumption baseline |
| `anumaan/care.py` | Beds from the admission-discharge feed; staff attendance checked against the care record |
| `anumaan/federation.py` | State nodes, the clean-room gate, the national view and shared priors |
| `anumaan/voice.py` | Gemini voice shelf check with a safe fallback |
| `anumaan/sim.py`, `anumaan/evaluate.py` | SYNTHETIC network with injected failures and a DVDMS-style warehouse ledger; held-out scoring against fair baselines |
| `grammar/` | Gemini grammar compiler; `crg/tracer.json` is a **hand-written development seed, not verified against guidelines** |
| `app/` | FastAPI service and the demo page |
| `deploy/`, `Dockerfile` | Cloud Run deployment and budget alert |

## Status against the Track 03 brief

| Requirement | Status |
|---|---|
| Real-time medicine stock visibility | Done on synthetic replay: the register and the inferred shelf, side by side |
| Bed availability | Done on synthetic replay (plain admission-discharge count first) |
| Staff attendance | Done on synthetic replay, by role only |
| Demand forecasting | Done: diagnosis-driven, with an uncertainty band |
| Early warning of stock-outs | Done: from staff rationing, and from the shadow stock where staff do not ration |
| Cross-district redistribution | Done: OR-Tools transfers within a state, plus escalations |
| Federated, shared modelling across states | Done as a code boundary that mirrors per-state projects: a national-shortage flag from the states' warehouse exports, and shared priors for new states |
| Google AI doing meaningful work | Gemini grammar and voice are built but not yet run; they need Vertex AI billing |
| Multilingual / voice | Odia buttons; a spoken answer in any language through Gemini once it is configured |
| Live deployed link | Pending billing on `anumaan-c4c` |

## Prior art we build on

Each piece has precedent. The combination is what we have not found, after about 600 searches across products, government systems, papers, patents and hackathon repos. The precedents:

- latent retail stock from sales (Montoya & Gonzalez 2019)
- stock-outs inferred from substitution (Anupindi et al. 1998)
- inaccurate inventory records (DeHoratius & Mersereau)
- morbidity-based quantification (MSH/WHO)
- the WHO/INRUD "% prescribed drugs dispensed" indicator
- decision-aware essential-medicine allocation in Sierra Leone (Chung, Bastani et al.)
- geographic shortage-scope classifiers (CisMED, Sun et al. 2025)

The combination is a guideline-compiled grammar used as the observation model to infer hidden PHC stock from frontline adaptation, with alarms clustered up the supply tree. We describe it as "first to combine", never "first ever".
