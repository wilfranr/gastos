# Control de gastos · Yoseth

Personal expense dashboard, packaged as a mobile-first progressive web app (PWA) in Spanish. It shows payment summaries, charts, and the payment list from `data.json`. Food receipts live in a separate file, `alimentacion.json`, and do not count toward service totals, the Pagos list, or the monthly services alert.

The payment data is public by design (no password). GitHub Pages URL (branch `main`, folder `/`): https://wilfranr.github.io/gastos/

## Rebuild

`gastos.csv` is the source file. After editing it, regenerate `data.json` from the repo root:

```bash
python3 build.py
```

`build.py` reads `gastos.csv` in this folder and writes `data.json`. Set `DASH_PASSWORD` only if you also want an encrypted `data.enc`. This published site uses the plain `data.json`.

## Alimentación

`alimentacion.json` is a JSON array at the repo root. Commit `[]` when there are no receipts yet. Each receipt looks like:

```json
{"fecha":"2026-10-15","anio":"2026","mes":10,"periodo":"2026-10","comercio":"Éxito","total":182350,"medio_pago":"Tarjeta","nota":""}
```

A missing file (404) or an empty array leaves the Alimentación view on its empty state. The page does not change `data.json`.
