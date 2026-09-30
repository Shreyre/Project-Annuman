# Anumaan

**The register says it's on the shelf. The care says otherwise.**

Anumaan ("inference") finds medicine stock-outs at Primary Health Centres that the stock register hides. It reads them from the care that is already being recorded. When a PHC runs out of a medicine, patients are still diagnosed, but full courses stop, courses get cut short, guideline substitutes appear and slips say "not available". Anumaan decodes those fingerprints against India's treatment guidelines to infer what is really on the shelf. It then plans what to move, what to escalate and what the next two weeks will need.

Built for **Build with AI: Code for Communities, Second Edition**, Track 03 (Smart Health & Supply Chain Resilience).

## Who benefits

- **31,882 PHCs**, 25,354 rural and 6,528 urban, on 31 March 2023 ([Health Dynamics of India 2022-23](https://web.archive.org/web/20240911061043/https://mohfw.gov.in/sites/default/files/Health%20Dynamics%20of%20India%20%28Infrastructure%20%26%20Human%20Resources%29%202022-23_RE%20%281%29.pdf), MoHFW, pp. 25 and 123).
- **About 90 crore people behind the rural ones alone.** An average rural PHC covers 35,602 people, against a norm of 30,000 in the plains and 20,000 in hilly and tribal areas; an urban PHC is planned for 50,000 (same report, pp. 28 and 125; [IPHS 2022](https://nhm.gov.in/images/pdf/guidelines/iphs/iphs-revised-guidlines-2022/03_PHC_IPHS_Guidelines-2022.pdf), Vol. III, p. 7).
- **Shelves are often short.** Studies put the share of essential medicines in stock at PHCs at 72-74% in a district of Puducherry ([Meena et al. 2021](https://pmc.ncbi.nlm.nih.gov/articles/PMC8654139/)) and one of Karnataka ([Tejesh & Nagaveni 2023](https://pmc.ncbi.nlm.nih.gov/articles/PMC10292169/)), and at about 48-51% across Punjab and Haryana ([Prinja et al. 2015](https://pmc.ncbi.nlm.nih.gov/articles/PMC4690305/)).
- **A short shelf costs the patient.** When the PHC has none, the patient buys it outside or goes without. Medicines are about 42% of what Indian households pay for health out of pocket: ₹1.61 lakh crore of ₹3.83 lakh crore in 2022-23. That is our sum of prescribed and over-the-counter medicines in the [National Health Accounts 2022-23](https://nhsrcindia.org/sites/default/files/2026-05/NHA%202022-23%20Report.pdf), Table A.3, and a floor, because medicines bought during a hospital stay count as inpatient care.

Anumaan asks PHC staff for no new data entry, so it can reach every PHC whose diagnoses and dispensing are already digital. Each state runs its own copy, so it grows a state at a time without pooling patient records, and the nightly compute for all 31,882 PHCs is about $1.7 a month (see Scale below).

## How it works

1. **Compile the guidelines.** Gemini reads ICMR Standard Treatment Workflows and national programme guidelines (NHSRC, NPCDCS, Anemia Mukt Bharat). It writes a cited *care-to-resource grammar*: for each diagnosis, the medicine course it should consume, the permitted substitutes, and the page it came from. A second Gemini pass drops any rule the PDF does not support. The demo runs on the grammar Gemini compiled from six official PDFs. See `grammar/compile_crg.py`.
2. **Infer the shelf.** For every PHC and medicine, a Bayesian filter compares the courses the diagnoses called for with what was dispensed: full, cut short, substituted, not available, or silently missing. See `anumaan/filter.py`.
   - The register is one more sensor, weighted by how well that PHC's register tracked reality in its first month.
   - A day with no slips entered on any medicine is a data-entry gap, not a stock-out.
   - A pharmacist's shelf check, by button or by voice, replaces the day's belief.
   - A **shadow stock** warns before the shelf empties even where staff never ration. It is the register's opening balance plus deliveries, minus what the dispensing slips took out. Registers post issues late or never; the slips are the issues. It is reset to zero whenever the care shows the shelf empty. Under 8 days of use left raises an alarm.
3. **Localise and act.** "Where it broke" is read from the warehouse ledger (DVDMS records each district warehouse's weekly indent to the state and what arrived). A warehouse whose indents the state has stopped filling is starved. One starved warehouse is a warehouse failure; a third of a state's warehouses, and at least two, is a state procurement failure; that in each of two states is national. If supply is still flowing, a state-wide jump in diagnoses marks a demand surge; otherwise the problem is at the PHC. Then Google OR-Tools plans same-state transfers in treatment courses from PHCs that have been calm all week, sized on their shadow stock, and escalates what moving stock cannot fix. See `anumaan/triage.py` and `anumaan/planner.py`.
4. **See beds, staff and demand.**
   - **Beds** come from the admission-discharge feed, with stays that were never closed auto-closed after 5 days.
   - **Staff** are checked against the care record, by role only: a doctor marked present but with no prescriptions on a 40-patient day gets flagged.
   - **Demand** for the next 14 days is forecast from diagnoses through the grammar, not from dispensing that stock-outs have already cut.
   - **The board** covers the whole network: which PHCs have every bed taken and the nearest PHC with a free bed by road, which attendance marks to check today, and which PHCs' attendance registers disagree with their care record most often.

   See `anumaan/care.py` and `anumaan/forecast.py`.
5. **Take the records as they arrive.** A PHC's day can come as one message: its diagnoses, dispensing slips, register balances, admissions and attendance. The PHC's estimates are recomputed when the message lands, and the result is the same as loading the whole history at once. See `anumaan/feeds.py` (`stream`, `absorb`, `publish`) and `/api/ingest`.
6. **Federate.** Each state keeps its own diagnoses, slips and warehouse ledger. Only counts per district and medicine cross the state line, plus whether the state has stopped supplying that district's warehouse. They pass a gate that rejects anything raw and any group under five PHCs. The national view flags a national shortage when starved warehouses show up in two or more states, and hands back shared starting parameters for new states. See `anumaan/federation.py`.

## Results so far: synthetic data only

There is no public patient-level dispensing data in India, so everything below comes from a simulator (`anumaan/sim.py`). It mimics the shape of the real feeds and injects failures as hidden ground truth. It is deliberately messy: part-filled indents, upstream failures that still trickle supply, 30-90 day warehouse buffers, a monsoon surge, data-entry gaps, registers that drift, substitutes that fail too, and a warehouse ledger posted 0-10 days late that loses 5% of receipts.

Thresholds were tuned on seeds 0-4 with the earlier hand-written grammar, and not re-tuned for the compiled one. Every number below is from **held-out seeds 5-9** on the compiled grammar. Each module's CLI reproduces its numbers.

**Detecting hidden stock-outs** (`python -m anumaan.evaluate --seeds 5-9`)

| Behaviour of frontline staff | Stock-outs of 4+ days caught | Warned before the shelf emptied | False alarms per medicine per PHC per year | Register tuned to the same false-alarm budget |
|---|---|---|---|---|
| Ration when stock runs low (default) | 98-100% | 91-98% (median 6-8 days ahead) | 0.00-0.04 | 7-50% caught |
| Different behaviour model, no "not available" slips (`--behaviour alt`) | 96-100% | 64-78% (2-4 days ahead) | 0.01-0.05 | 32-55% caught |
| Never ration (`--ration 0`) | 95-99% | 54-61% (2 days ahead) | 0.01-0.04 | 4-45% caught |

Where staff do not ration, the warning comes from the shadow stock. The care record alone warns for only 20-39% (alt) and 2-5% (never ration) of stock-outs, and catches 64-70% of all stock-outs instead of 85-94% when staff never ration. Without diagnoses (the same filter and shadow stock run on each medicine's dispensing history) Anumaan catches as many stock-outs or more, but with 20x or more false alarms.

Catching a stock-out at some point is one thing; knowing on a given day that the shelf is empty is harder. Day by day after the 30-day warm-up, on the days a shelf was truly empty, Anumaan called it empty on 57-65% of them and was right 96-99% of the times it said so. The register showed under half a day of use on 0-5% of those days.

**Where it broke** (same runs, labelled 7 days after the alarm)

| Behaviour of frontline staff | Stock-outs labelled right | Right on average per cause | Always guessing the commonest cause |
|---|---|---|---|
| Default | 78-90% | 72-91% | 36-47% |
| Alt | 87-97% | 78-95% | 42-54% |
| Never ration | 85-94% | 76-92% | 45-53% |

Before the warehouse ledger, clustering alarms up the supply tree scored 36-56% on default (with the hand-written grammar): no better than guessing.

"A third of a state's warehouses" is the same as "two or more" in these 3-warehouse states, so nothing above changed when the rule was generalised. It matters for a state with many districts, where a lost ledger posting starves a warehouse by chance. On tuning seeds 0-4 with 14 warehouses a state, "two or more" labelled 72-89% of alarms right and called 116 ordinary stock-outs state or national; a third labels 85-90% right, calls 18, and misses 2 more real ones.

**A world as short of stock as the surveys say** (`python -m anumaan.evaluate --seeds 5-9 --fill 0.05 0.3`)

The default simulated shelves are stocked 98-99% of the time, while surveys of Indian PHCs find 48-74% (see Who benefits). Cutting the share of each routine indent the warehouse sends, from 50-100% to 5-30%, brings the simulated shelves down to 65-75% stocked, with 1,600 to 1,900 stock-outs a run instead of about 90. Nothing in the model was changed or re-tuned for it.

| Behaviour of frontline staff | Shelves stocked | On the days a shelf was empty, Anumaan said so | When it said so, it was right | The register showed it |
|---|---|---|---|---|
| Default | 74-75% | 70-72% | over 99% | 1-3% |
| Alt | 70-72% | 64-65% | over 99% | 1-3% |
| Never ration | 65% | 74-75% | over 99% | 2-3% |

Two limits show up in this world. "Stock-outs caught" stops meaning much, because an alarm is live most of the time: every method scores high on it, the register included (85-88% at the same false-alarm budget). And "where it broke" is right for 73-79% of alarms, no better than always answering "at this PHC" (75-80%), because four stock-outs in five are now routine under-supply; averaged per cause it falls to 41-46%.

**The other modules**

| Module | Held-out result | What we are not claiming |
|---|---|---|
| Beds (`python -m anumaan.care`) | Count off by 0.28-0.37 beds on average, against 2.1-2.3 for plain admitted-minus-discharged | A quarter to a third of "every bed taken" days are artefacts of unrecorded discharges |
| Staff | "Marked present, no work on a busy day" flags are right 94-96% of the time and catch 70-75% of false attendance. Pooled per PHC, the same evidence ranks PHCs by how often they mark absent staff present: rank correlation 0.96-0.98 with each PHC's true rate, against 0.13-0.59 for the attendance feed alone | A simple ratio rule does nearly as well on both; staff nurses are too rarely busy to check (recall about 0) |
| Forecast (`python -m anumaan.forecast`) | 14-day demand error (WAPE) 0.072-0.075 from diagnoses, against 0.116-0.126 for consumption with stock-out days filled in, and 0.085-0.093 for zero-shot TimesFM on BigQuery (`--bigquery`) | Part of the edge is built in, because the simulator defines demand as diagnoses times the grammar. After stock-outs the lead is only 1.0-1.4x. Neither method sees the monsoon coming |
| Redistribution (`python -m anumaan.planner`) | On real road times: 68-90% of planned courses go to PHCs that are truly short (a register-driven plan manages 38-56%); 84-92% come from PHCs with true surplus; it meets 49-68% of the need (register plan 31-60%). 82-98% of escalations are true state or national failures | Plans are never applied to the simulated world, so each week re-plans the same shortages. The road times are real, but between synthetic PHC locations; 5 of the 180 PHCs have no road route and fall back to a straight-line estimate |
| Scale (`python -m anumaan.scale`) | One state of 1,500 PHCs x 7 medicines over 200 days: the nightly learn-and-filter of all 10,500 series takes 41 s on one laptop core (3.9 ms per series, measured while other jobs ran), the day's "where it broke" labels 2.4 s, and the whole state's OR-Tools plan 0.3 s. For India's 31,882 PHCs (MoHFW, 2023) x 25 tracer medicines that is about 52 vCPU-minutes a night, about $1.7 a month of Cloud Run CPU | Compute only: ingesting the feeds, storage and serving are not measured. The filter is per series, so it shards by state |
| Federation (`python -m anumaan.federation`) | About 4 KB of counts leave each state instead of about 130,000 raw records; the gate rejects doctored exports. The national-shortage flag caught **5 of 5** injected national failures with no flags on other medicines, ahead of 75 of the 84 PHC stock-outs they caused. Shared priors cut a 3-day-old state's false alarms from 0.04 to 0.02 per medicine per PHC per year (fewer in 7 of 10 cold states, more in 1), the same as a 30-day warm-up | The flag goes up 26-30 days into a failure, after the first PHCs have run out. The priors gain is small in absolute terms: the care filter needs little calibration, and the gain comes through the shadow stock's days of use |
| Live feed (`tests/test_app.py`, `tests/test_feeds.py`) | Fed one PHC's day at a time, the live network shows exactly what a replay of the same records shows: every cell, the beds and staff board and the states' exports. A PHC's seven series are re-filtered in about 5 ms when its report lands (median 4.5 ms, 95th percentile 6.2 ms on a laptop), so a 36-PHC day takes about 0.2 s | The demo's feed is the simulator played forward, not a state's system. One instance holds the network in memory, so a restart starts the feed again from its first 60 days |
| Kerala replay (`python -m anumaan.scenario`) | A state procurement failure scripted on Kerala's 14 districts: supply of three medicines stops on day 35; by day 53 the ledger shows it in 8 of 14 districts and the alarms are labelled state procurement; the first PHC shelf empties on day 54, and all 300 stock-outs the failure goes on to cause come after the call | One scripted run with synthetic records, not a test set: it shows what the verdict looks like, not how often it is right. The held-out seeds above do that |

What we are **not** claiming overall:

- **The shadow stock is only as good as its first balance and the slips.** It starts from the register's opening balance, so an overstated opening delays the first warning until the care shows an empty shelf and resets it. A shelf count at go-live fixes that. A PHC that dispenses without writing slips would fool it.
- **"Where it broke" needs the warehouse ledger.** The simulator's ledger is cleaner than a real DVDMS feed: late and lossy, but otherwise exact. A state without one gets only "at this PHC" or "demand surge". The scorer counts a stock-out during an upstream failure as that failure even when its own warehouse still had stock; the ledger sees those failures because the indents stop being filled.
- **The simulator is kinder than reality.** Its shelves are stocked 98-99% of the time, while studies of Indian PHCs find 48-74% of essential medicines in stock (see Who benefits). Made that short of stock, it still names the empty shelves, but "where it broke" is no better than a guess (see above). Even then the simulated world starts with full shelves, so the month the filter learns from is clean; a PHC already short in its first month is not tested.
- **The filter has not been validated on real data.** The check on England's dispensing records ([below](#a-first-check-on-real-data)) tests only the premise that shortages leave these fingerprints. A pilot would check against real signals first, such as HMIS "discharged under 48 hours", state drug-availability dashboards and DVDMS warehouse records. [Pilot in four weeks](#pilot-in-four-weeks) sets out how.

## A first check on real data

India publishes no dispensing data at the grain Anumaan reads. England does: the NHS Business Services Authority's English Prescribing Dataset records what is dispensed each month against every GP practice's prescriptions. `python -m anumaan.realcheck` asks one thing of it. When a medicine was officially declared short, did the fingerprints Anumaan reads show up in routine dispensing records, in many areas at once?

An area is one of England's 42 integrated care boards. It is flagged when it moves more than 3 robust standard deviations from its own 12 months before the first official notice. The shortage counts as wide when two or more areas are flagged in each of two or more NHS regions, which is the rule `anumaan/triage.py` uses for a national failure. The thresholds were fixed before the data was looked at.

| Declared shortage | First official notice | Substitutes' share of items up: most areas flagged | Courses cut short: most areas flagged | Wide, first met |
|---|---|---|---|---|
| Creon capsules (pancreatic enzymes), 2024 | February 2024 | 28 of 42, in September 2024 | 26 of 42, in September 2023; 5 at most after the notice | September 2023, five months before the notice |
| Oestrogel (estradiol gel), 2022 | April 2022 | 41 of 42, in March 2022 | None | February 2022, two months before the notice |
| Two medicines with no declared shortage, over the same months | | 1 of 42 | 7 of 42 | Never for substitution. For cut-short courses, in 1 of 100 months, as azathioprine's tablets per item drifted down |

What this shows, and what it does not:

- **The fingerprints are there in real records.** Substitution showed in both shortages and cut-short courses in one, each in many areas at once. The medicines with no shortage stayed quiet on substitution.
- **Both times the first signs came before the first official notice we found.** That is not proof of early warning. Those months fall inside each area's own baseline year and are scored in hindsight, against the other eleven months, later ones included. The makers reported supply trouble covering those months. And the data came out about two months in arrears.
- **The notices ordered part of what followed.** The February 2024 notice asked prescribers to switch Creon strengths and supply a month at a time, and from May 2022 pharmacists could supply patches in place of Oestrogel. Oestrogel's was also a demand-led shortage: its items rose about threefold over the two years.
- **The Creon figures lean on two choices.** Both were fixed before the results were seen: capsules are counted by strength, and each area's spread has a floor. With the floor doubled, substitution peaks at 11 areas, not 28. Counted in plain capsules, the cut-short dips vanish. The Oestrogel result barely moves under either.
- **It tests the premise, not Anumaan.** The filter was not run on this data. These are monthly totals per area, in another health system, for two shortages picked by hand.

The notices, their sources and the full list of caveats are in `anumaan/realcheck_england.json`, and the Performance tab of the demo shows the same table. Contains public sector information licensed under the Open Government Licence v3.0.

## Run it

```bash
pip install -e ".[app,gemini,dev]"                 # numpy, ortools, fastapi, uvicorn, google-genai, pytest
python -m pytest -q                               # 40 checks, incl. held-out end-to-end runs and the app
python -m anumaan.evaluate --seeds 5-9            # detection table; also --behaviour alt, --ration 0, --fill 0.05 0.3
python -m uvicorn app.main:app --port 8788        # demo at http://127.0.0.1:8788
python -m anumaan.scenario                        # the Kerala replay's alarms, week by week
python -m anumaan.realcheck                       # the check on England's open dispensing records
python -m anumaan.feeds export sample/            # the synthetic network as the CSV files a state would export
python -m anumaan.feeds run sample/               # the day's alarms, full PHCs and attendance to check, from those files
```

Live demo: <https://anumaan-336541806157.asia-south1.run.app> (Cloud Run, Mumbai). It scales to zero, so the first visit after a quiet spell is slower.

The demo has three networks, switched at the top of the page. All three are synthetic.

- **Demo network.** 36 PHCs, 6 warehouses and 2 states over 200 days, on held-out seed 5. Scrub or play through the days.
- **Kerala replay** (`/?net=kerala`). The same simulator with its failures scripted: 70 PHCs on Kerala's 14 districts, where the state stops filling warehouse indents for three medicines on day 35. It follows two Onmanorama reports of September 2026: that companies had shown no interest in supplying 180 of the items in KMSCL's tender for the year ([22 September](https://www.onmanorama.com/news/kerala/2026/09/22/kmscl-supply-crisis-delays-hospitals-medicine-scarcity-kerala.html)), and that hospitals still had stock while KMSCL's warehouses were running short ([29 September](https://www.onmanorama.com/news/kerala/2026/09/29/medicine-stocks-available-at-hospitals-shortage-at-kmscl-warehouses-dmos-inform-health-minister.html)). The records are not KMSCL's, and the three medicines are this project's tracers: KMSCL has not published which items drew no bids. It opens on day 70, when the verdict is in and most shelves have not emptied yet.
- **Live feed** (`/?net=live`). The same pipeline fed one PHC's day at a time. "Start feed" sends the next day's reports, one message per PHC, and each PHC's estimates are recomputed as its report lands. The page says whether the messages travelled through Pub/Sub or were applied directly.

On any of them:

- **Select a PHC and medicine** to see the register against the inferred shelf, the evidence, "where it broke", Gemini's brief for the district officer (English, Odia, Hindi or Malayalam), the pharmacist check (buttons in English and Odia, or a recorded answer), the 14-day forecast, and that PHC's beds and staff.
- **Redistribution** lists today's transfers by road time (Approve turns one into an issue order in the ledger) and the shortages to escalate instead. Road times are Google Maps drive times in the demo network and the live feed, and straight-line estimates in the Kerala replay.
- **Beds & staff** shows where a full PHC can send its next patient, which attendance marks to check today, and which PHCs' registers to audit.
- **National view** shows what crosses the state line, and **Performance** the held-out scores.
- **Turn on "Ground truth"** to compare with what the simulator hid.

## Google AI and deployment

| Piece | Where | Status |
|---|---|---|
| Guideline grammar, Gemini 3.7 Flash (PDF in, JSON schema out, verification pass) | `grammar/compile_crg.py` | Run on 6 official PDFs (`grammar/sources/urls.txt`). `grammar/crg/compiled.json` has a cited course for each of the 7 tracer conditions under the seed's ids, and every quote is on its cited page. On held-out seeds 5-9 it catches 96-100% of stock-outs (seed grammar: 99-100%) with fewer false alarms. The demo runs on it |
| Voice shelf check, Gemini 3.7 Flash (audio in, answer and cause out) | `anumaan/voice.py`, `/api/voice` | Live on Vertex AI: a spoken test clip was transcribed exactly and read as `empty`, locally and on Cloud Run |
| Forecasting on BigQuery AI.FORECAST with TimesFM | `anumaan/forecast.py` (`--bigquery`) | Run: one zero-shot AI.FORECAST query backtests 13,860 series from held-out seeds 5-9, in asia-south1. It trails the local damped-trend ETS (WAPE 0.085-0.093 against 0.072-0.075) because it forecasts the daily median, which sits under the mean for sparse counts, so the demo keeps the ETS |
| Routing with Google Maps Routes, redistribution with Google OR-Tools | `anumaan/planner.py`, `anumaan/road_minutes.json` | Live: Routes API `computeRouteMatrix` road times for the 3,060 same-state pairs of seeds 5-9, cached so the app makes no runtime call. OR-Tools plans on them, and the district officer's Approve turns a transfer into a DVDMS-style issue order (`/api/approve`, `/api/ledger`) |
| District officer's brief, Gemini 3.7 Flash (the alarm's evidence in; two or three sentences and a next step out, in English, Odia, Hindi or Malayalam) | `anumaan/voice.py` `write_brief`, `/api/brief` | Live on Vertex AI. Gemini calls are capped per day on the demo, and the same evidence never pays twice |
| The same compiler on another country's guideline | `grammar/crg/za_compiled.json` | South Africa's Primary Healthcare STGs and EML (2024): 6 of the 7 tracer courses, every quote on its cited page. Hypertension starts on hydrochlorothiazide there and on amlodipine in India. Its UTI rule (a gentamicin injection first) needs a clinician's check |
| Federation on BigQuery | `anumaan/cleanroom.py`, `deploy/national.sh` | Live in asia-south1. Each state has a private dataset and an authorized view that returns only warehouse x medicine x day rows with at least 5 PHCs behind them; a national service account can read those views and nothing else. `python -m anumaan.cleanroom proof` checks it: all 8,400 national rows match the Python export, and raw tables and per-PHC queries are refused. An aggregation-threshold policy over per-PHC rows was tried first and dropped: differencing 16 aggregate queries still singled out one PHC |
| Live feed: Pub/Sub, pushed to Cloud Run | `anumaan/feeds.py` (`stream`, `publish`), `/api/ingest`, `deploy/live.sh` | The service publishes each PHC's day to a topic, and a push subscription delivers every message back to `/api/ingest`, which lets one in only with the feed's token. Until `deploy/live.sh` has been run on a copy, the same messages are applied in-process, and the page says which it was |
| Cloud Run deploy, budget alert | `Dockerfile`, `deploy/deploy.sh`, `deploy/budget.sh` | Live in asia-south1: one instance at most, because shelf checks, approvals and the live feed are held in its memory, and scaling to zero when idle; ₹2,000/month budget alert set |

To deploy your own copy:

1. Link a billing account to GCP project `anumaan-c4c`.
2. Run `gcloud auth application-default login`.
3. Run `bash deploy/budget.sh <billing-account> 2000`, then `bash deploy/deploy.sh`.
4. Compile the real grammar. Download the PDFs listed in `grammar/sources/urls.txt` into that folder and load `.env` into the shell. Pass the PDFs in priority order, because the first PDF wins a condition+drug pair:

   ```bash
   S=grammar/sources; python grammar/compile_crg.py $S/stw_pneumonia.pdf $S/stw_hypertension.pdf $S/stw_uti.pdf \
     $S/amb_anaemia_pregnancy.pdf $S/npcdcs_diabetes.pdf $S/nhsrc_cho_fever_diarrhoea.pdf -o grammar/crg/compiled.json
   ```

5. The federation on BigQuery: `bash deploy/national.sh` once, then `python -m anumaan.cleanroom onboard S0` and `onboard S1` (one command per state; in production each state runs it in its own project with `--project`), then `python -m anumaan.cleanroom proof`.
6. The live feed on Pub/Sub: `bash deploy/live.sh`, once the service is deployed.
7. Optional cloud checks: `python -m anumaan.forecast --bigquery anumaan-c4c.anumaan_forecast` backtests TimesFM, and `python -m anumaan.planner --seeds 5-9 --fetch-routes` refreshes the road times (needs the Routes API enabled).

## Pilot in four weeks

A pilot runs on what a state already records; PHC staff get no new app to fill in. `anumaan/feeds.py` reads those records as one CSV file per feed and runs the demo's pipeline on them (`python -m anumaan.feeds --help` lists the columns). Exported to CSV and read back, seed 5 gives exactly the demo's alarms and "where it broke" labels, and `tests/test_feeds.py` keeps it that way.

| Feed | Where it lives today | Needed |
|---|---|---|
| PHCs, with their district warehouse and state | The state's facility list | Yes |
| Diagnoses per PHC per day | The OPD register, digitised where the PHC runs a hospital information system | Yes |
| Dispensing slips (medicine, days, units per prescription) | The state's drug distribution system (DVDMS or e-Aushadhi) where the PHC issues through it, otherwise the pharmacy's prescription register | Yes |
| Stock register and receipts | The PHC store's stock book, in the same system | Yes; a shelf count on day one where there is none |
| "Not available" slips | Pharmacy notes | Optional |
| Warehouse indents and what arrived | DVDMS at the district warehouse | Optional; without it "where it broke" is only "at this PHC" or "demand surge" |
| Beds and posts filled; admissions and discharges | The PHC's staff list; e-Hospital, or the labour-room and inpatient registers | Optional; without them there is no beds and staff board |
| Attendance marks, with each role's acts and the day's patients | AEBAS or the attendance register; the acts come from the same care record | Optional, with the beds files |

The hard requirement is digital diagnoses and dispensing. A PHC still on paper registers can use the shelf check (buttons or voice) but not the inference, so a pilot starts in a district where both are already digital.

| Week | What happens | Who |
|---|---|---|
| 1 | Pick the district. Map the state's drug and diagnosis codes to the grammar's ids, and recompile the grammar from the state's own treatment guidelines if they differ (one `compile_crg.py` run). Stand up the state's own project: `deploy/deploy.sh`, then `cleanroom onboard <state>` | State IT cell, with us |
| 2 | Load at least 30 days of history: the filter learns each register's trust and each PHC's prescribing rate from the first 30. A shelf count at every PHC gives the shadow stock a true opening balance | District pharmacist |
| 3 | Shadow mode: `feeds run` every night, with alarms going to the pilot team only. The PHC pharmacist checks each alarm at the shelf, by button or voice, which gives the first real labels | PHC pharmacists |
| 4 | Score the alarms against those checks and the state's drug-availability dashboard, and set the alarm threshold for the district. Then switch on the district officer's brief and transfer approvals | District officer, state |

The pilot reports three numbers: the share of alarms the shelf confirmed, the stock-outs the shelf found with no alarm, and how many days ahead the alarms came. A second state repeats the four weeks in its own project and joins the national view with one `onboard`.

Once a district is running, its PHCs' systems can send each day as a message instead of a nightly file. `python -m anumaan.feeds publish <folder> --topic projects/P/topics/T` sends a folder's day to a Pub/Sub topic, one message per PHC, in the format `/api/ingest` takes.

## Layout

| Path | What |
|---|---|
| `anumaan/crg.py` | Loads the grammar; decodes slips into full / cut short / substitute / not available |
| `anumaan/filter.py` | Per PHC x medicine Bayesian filter, data-entry gaps, learned register trust, shelf checks, shadow stock |
| `anumaan/triage.py` | Where it broke, from the warehouse ledger's unfilled indents and state-wide demand |
| `anumaan/planner.py` | OR-Tools min-cost-flow transfers in treatment courses, donors sized on the shadow stock, escalations, DVDMS-style orders; `road_minutes.json` caches the Google Maps road times |
| `anumaan/forecast.py` | Diagnosis-driven demand forecast and the fair consumption baseline; `--bigquery` backtests TimesFM on BigQuery AI.FORECAST |
| `anumaan/care.py` | Beds from the admission-discharge feed; staff attendance checked against the care record |
| `anumaan/federation.py` | State nodes, the clean-room gate, the national view and shared priors |
| `anumaan/cleanroom.py`, `deploy/national.sh` | The same boundary on BigQuery: a private dataset per state, a shared view, a national service account; `onboard` adds a state |
| `anumaan/scale.py` | Load test of one large state, and the arithmetic for all of India |
| `anumaan/feeds.py` | A state's CSV exports in (medicines, beds, staff), the day's alarms and where each broke out; `export` writes the synthetic network in the same format; `stream`, `absorb` and `publish` carry the same rows as a live feed |
| `anumaan/realcheck.py`, `anumaan/realcheck_england.json` | The premise checked on real dispensing records: England's open prescribing data around two declared shortages, cached with its sources |
| `anumaan/scenario.py` | Scenario replays: the simulator with its failures scripted from a reported event, on a real state's districts. Holds the Kerala replay |
| `anumaan/voice.py` | Gemini voice shelf check with a safe fallback, and the district officer's brief |
| `anumaan/sim.py`, `anumaan/evaluate.py` | SYNTHETIC network with injected failures and a DVDMS-style warehouse ledger; held-out scoring against fair baselines |
| `grammar/` | Gemini grammar compiler; `crg/compiled.json`, which the demo runs on, is compiled from the official PDFs listed in `sources/urls.txt`; `crg/tracer.json` is the **hand-written seed** that gives the compiler its condition and drug names |
| `app/` | FastAPI service and the demo page |
| `deploy/`, `Dockerfile` | Cloud Run deployment, the live feed's Pub/Sub topic and push subscription, and the budget alert |
| `NOTICE` | Third-party software, the Google services used, the source documents behind the grammar, and the data and reports the real-data check and the Kerala replay draw on |

## Status against the Track 03 brief

| Requirement | Status |
|---|---|
| Real-time medicine stock visibility | Done on synthetic data: the register and the inferred shelf, side by side, in a replay and in a live feed that updates each PHC as its day's report arrives |
| Bed availability | Done on synthetic data (plain admission-discharge count first): a board for the whole network, with the nearest free bed by road for a full PHC. In the CSV feeds and the live feed |
| Staff attendance | Done on synthetic data, by role only: today's marks to check, and the PHCs whose registers to audit. In the CSV feeds and the live feed |
| Demand forecasting | Done: diagnosis-driven, with an uncertainty band, and backtested against TimesFM on BigQuery AI.FORECAST |
| Early warning of stock-outs | Done: from staff rationing, and from the shadow stock where staff do not ration |
| Cross-district redistribution | Done: OR-Tools transfers within a state over real road times, one-click approval into an order ledger, plus escalations. The Kerala replay shows the case where redistribution is the wrong answer |
| Federated, shared modelling across states | Done on BigQuery: each state keeps its PHC rows in its own dataset and shares one warehouse-level view, and the national service account can read only those views. On top of that, a national-shortage flag from the states' warehouse exports, and shared priors for new states |
| Google AI doing meaningful work | Gemini voice is live on Vertex AI; the demo runs on the grammar Gemini compiled from 6 official guideline PDFs |
| Multilingual / voice | Odia buttons; spoken answers in any language, and the district officer's brief in English, Odia, Hindi or Malayalam, through Gemini on Vertex AI |
| Live deployed link | <https://anumaan-336541806157.asia-south1.run.app> (synthetic data) |

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
