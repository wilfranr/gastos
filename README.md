# Control de gastos · Yoseth

Personal expense dashboard, packaged as a mobile-first progressive web app (PWA) in Spanish. It shows payment summaries, charts, and the payment list from `data.json`.

The payment data is public by design (no password). GitHub Pages URL (branch `main`, folder `/`): https://wilfranr.github.io/gastos/

## Rebuild

`gastos.csv` is the source file. After editing it, regenerate `data.json` from the repo root:

```bash
python3 build.py
```

`build.py` reads `gastos.csv` in this folder and writes `data.json`. Set `DASH_PASSWORD` only if you also want an encrypted `data.enc`. This published site uses the plain `data.json`.
