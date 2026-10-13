# legacy/

Code and documents kept so that they can be read, not run. Nothing under this directory is
imported by the pipeline or by the live parts of `modules/rubrat/`, nothing here is maintained,
and the Python files carry a `DEPRECATED` header naming their origin. Imports inside them refer to
the layouts they came from and do not resolve here.

## rubrat_sim_builders/

From RuBR-AT (Aayush Kuloor, commit `3e665de`): the dataset builders and truth matching for the
OpenUniverse HLTDS and RImTimSim GBTDS simulations the published model was trained on
(`Roman_TDS` truth files, the RImTimSim source catalog, DAOStarFinder candidate catalogs), plus
`rubr_adapter_full.py`, the untrimmed original of `preprocessing/rubr_adapter.py`.

The retrain on SOC simulations uses SExtractor candidates and the per-field injection catalogs
(`s3://rapid-pipeline-files/injection_catalogs_<date>/`), so a new shard builder and truth matcher
will be written in `preprocessing/`. These files show the NPZ contract and matching conventions
to carry over.

## dann/

From `rapid/RuBR/` (Ashish Mahabal, commit `90fbc83b`, 2026-05-29): a domain-adversarial (DANN)
training experiment and its control baseline, with the `model/` subpackage they import. They
target the earlier six-feature rotation-invariant model, not the RuBR-AT model in `model/`, and
are kept because domain adaptation between simulated and flight data is on the plan; the
RuBR-AT package's own answer to that problem is the survey token plus positive-unlabeled training
(`training/pu.py`).

## rubrat_docs/

RuBR-AT's documentation as of commit `3e665de`: the dataset and model cards, the paper's
reproduction recipe, the original integration guide, its dependency pins, and a fine-tuning
notebook. `data.md` holds the definitive prose statement of the nine feature formulas and the
cutout convention that `preprocessing/` must keep satisfying. The maintained description of the
real/bogus stage belongs in RAPID's Sphinx documentation under `docs/source`.
