# Anumaan

**The register says it's on the shelf. The care says otherwise.**

Anumaan ("inference") finds medicine stock-outs at Primary Health Centres that the stock register hides. It reads them from the care that is already being recorded. When a PHC runs out of a medicine, patients are still diagnosed, but full courses stop, courses get cut short, guideline substitutes appear and slips say "not available". Anumaan decodes those fingerprints against India's treatment guidelines to infer what is really on the shelf.

Built for **Build with AI: Code for Communities, Second Edition**, Track 03 (Smart Health & Supply Chain Resilience).

## How it works

1. **Compile.** Gemini reads ICMR Standard Treatment Workflows, NHSRC guidelines and the Essential Medicines List. It writes a cited *care-to-resource grammar*: for each diagnosis, the medicine course it should consume, the permitted substitutes, and the page it came from. This is `grammar/compile_crg.py`, and a second Gemini pass drops any rule the PDF does not support.
2. **Infer.** For every PHC and medicine, a Bayesian filter compares the courses the diagnoses called for with what was dispensed: full, cut short, substituted, not available, or silently missing. The register is one more sensor, weighted by how well that PHC's register tracked reality in its first month. Days with no slips entered on *any* medicine count as a data-entry gap, not a stock-out. A pharmacist's shelf check replaces the day's belief. This is `anumaan/filter.py`.
3. **Localise.** Alarm onsets for the same medicine are clustered up the supply tree (PHC, warehouse, state medical services corporation, national). If diagnoses jumped while deliveries kept arriving, the alarm is labelled a demand surge. The label routes the fix: redistribute, audit the warehouse, escalate procurement, or raise indents. This is `anumaan/triage.py`.

## Results so far: synthetic data only

There is no public patient-level dispensing data in India, so everything below comes from a simulator (`anumaan/sim.py`). The simulator mimics the shape of the real feeds and injects failures as hidden ground truth. It is deliberately messy: part-filled indents, upstream failures that still trickle supply, 30 to 90 day warehouse buffers, a monsoon surge, data-entry gaps, registers that drift, and substitutes that fail too. The filter, triage and baseline thresholds were tuned on seeds 0 to 4. The numbers below are from **held-out seeds 5 to 9**, reproducible with `python -m anumaan.evaluate`.

| Behaviour of frontline staff | Stock-outs of 4+ days caught | Warned before the shelf emptied | False alarms per medicine per PHC per year | Register tuned to the same false-alarm budget |
|---|---|---|---|---|
| Ration when stock runs low (default) | 98-100% | 86-96% (median 4-7 days ahead) | 0.02-0.07 | 9-35% caught |
| Different behaviour model, no "not available" slips (`--behaviour alt`) | 96-98% | 31-37% | 0.03-0.07 | 21-59% caught |
| Never ration (`--ration 0`) | 92-97% | 3-8% (about 2 days *after*) | 0.01-0.04 | 5-41% caught |

The **diagnoses earn their place**. Running the same filter on each medicine's dispensing history alone catches as many stock-outs, but with 20x or more false alarms (1.2-1.8 per medicine per year).

What we are **not** claiming:

- **Early warning comes from staff rationing before the shelf empties.** Where they don't ration, Anumaan still finds the stock-out, but about two days after it starts.
- **"Where it broke" is a first guess, not a validated verdict.** On held-out seeds it is right for about 35-60% of caught stock-outs. That is no better than always guessing the commonest cause. Warehouse failures and demand surges are labelled reasonably well. State and national failures mostly are not, because warehouses with different buffers run dry weeks apart. The likely fix is warehouse stock and indent/issue records from DVDMS.
- **The simulator's shelves are stocked 97-99% of the time.** Indian PHC surveys report 72-75%. Real performance must be measured on real data.
- **Every number here was measured on synthetic data.** A pilot would validate against real signals first, such as HMIS "discharged under 48 hours" and state drug-availability dashboards.

## Run it

```bash
pip install numpy fastapi uvicorn pytest          # google-genai too, for the grammar compiler
python -m pytest -q                               # 3 checks, incl. a held-out end-to-end run
python -m anumaan.evaluate --seeds 5-9            # the table above; try --behaviour alt, --ration 0
python -m uvicorn app.main:app --port 8788        # demo at http://127.0.0.1:8788
```

The demo replays a synthetic network of 36 PHCs, 6 warehouses and 2 states over 200 days. Scrub or play through the days. Select a PHC and medicine to see the evidence, where it broke, and the pharmacist check (English and Odia). Tick "Show ground truth" to compare with what the simulator hid.

Compile a real grammar with Gemini on Vertex AI (settings come from `.env`):

```bash
gcloud auth application-default login
python grammar/compile_crg.py path/to/icmr_stw.pdf -o grammar/crg/compiled.json
```

## Layout

| Path | What |
|---|---|
| `anumaan/crg.py` | Loads the grammar; decodes slips into full / cut short / substitute / not available |
| `anumaan/filter.py` | Per PHC x medicine Bayesian filter, data-entry gaps, learned register trust, shelf checks |
| `anumaan/triage.py` | Supply-tree onset clustering and demand-surge detection |
| `anumaan/sim.py` | SYNTHETIC network with injected failures (ground truth) |
| `anumaan/evaluate.py` | Held-out scoring against the register, a receipt-gap rule, a threshold rule and a no-diagnosis ablation |
| `grammar/compile_crg.py` | Gemini compiler: guideline PDF to cited grammar, with a verification pass |
| `grammar/crg/tracer.json` | **Development seed, hand-written and not verified against guidelines.** To be replaced by compiled output |
| `app/` | FastAPI service and the demo page |

## Status against the Track 03 brief

| Requirement | Status |
|---|---|
| Real-time medicine stock visibility | Done on synthetic replay (register and inferred shelf, side by side) |
| Early warning of stock-outs | Done, with the rationing caveat above |
| Google AI doing meaningful work | Grammar compiler written for Gemini 3.7 Flash; **not yet run** (needs Vertex AI billing) |
| Multilingual / voice | Odia shelf-check buttons; Gemini voice confirmation not yet built |
| Bed availability, staff attendance | Not yet built |
| Demand forecasting | Not yet built (planned: BigQuery AI.FORECAST / TimesFM) |
| Cross-district redistribution | Recommended action only; the course-based planner is not yet built |
| Federated, shared modelling across states | Not yet built (planned: per-state projects, BigQuery clean room, shared priors) |
| Live deployed link | Pending. The Cloud Run deploy needs a billing account on GCP project `anumaan-c4c` |

## Prior art we build on

Each piece has precedent. The combination is what we have not found anywhere, after about 600 searches across products, government systems, papers, patents and hackathon repos. Precedents:

- latent retail stock from sales (Montoya & Gonzalez 2019)
- stock-outs inferred from substitution (Anupindi et al. 1998)
- inaccurate inventory records (DeHoratius & Mersereau)
- morbidity-based quantification (MSH/WHO)
- the WHO/INRUD "% prescribed drugs dispensed" indicator
- decision-aware essential-medicine allocation in Sierra Leone (Chung, Bastani et al.)
- geographic shortage-scope classifiers (CisMED, Sun et al. 2025)

The combination is: a guideline-compiled grammar used as the observation model to infer hidden PHC stock from frontline adaptation, with alarms clustered up the supply tree. We describe it as "first to combine", never "first ever".
