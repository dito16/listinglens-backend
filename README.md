# ListingLens

**Python-powered real estate intelligence and listing comparison.**

[Product landing page](https://edgegroup.site/) · [Hosted demo (Render Free)](https://listinglens-q3o1.onrender.com/)

ListingLens is a working Python/FastAPI project for collecting publicly accessible MyHome.ge property listings, comparing owner and agent listings, finding potential duplicates using computer vision, and generating PDF property sheets.

> **Hosted-demo limitation (21 October 2026):** The web interface and API are deployed, but MyHome.ge returned HTTP **403** to the Render host during live collection and PDF generation. As a result, those live operations **do not currently work on Render**. The online URL demonstrates the UI and deployment, **not** a fully functioning live MyHome integration. The program can be run on a user's own machine, although access there also depends on MyHome.ge allowing requests. No simulated data is substituted for a rejected live request.

## Features

| Workflow | What it does |
| --- | --- |
| **01 · Collect + Origin Match** | Fetch source listings, filter and export the results, and compare suspected owner/agent duplicates. |
| **02 · Search by ID** | Start from a MyHome listing ID, extract property attributes and search for potential copies. |
| **03 · PDF Generator** | Generate property sheets for 1–10 distinct listing IDs; download individual PDFs, a combined PDF or a ZIP. |

Image comparison uses histogram similarity and perceptual hashing (**pHash**), with **SIFT + RANSAC** as a separate geometric verification method. Matching is evidence-based; it is not a guarantee that two advertisements describe the same property.

The application uses HTML/SSR responses from the source website and does not require an external scraping API key. It does not bypass CAPTCHA challenges, authentication or HTTP access denials.

## Run locally

### Requirements

- **Python 3.12** recommended (see [`.python-version`](.python-version)).
- Git and an internet connection for dependency installation.
- Network access to MyHome.ge is needed for *live* tasks.
- On the first installation, allow time for NumPy, OpenCV and the other Python packages.

### macOS / Linux

```bash
git clone https://github.com/dito16/listinglens-backend.git
cd listinglens-backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m uvicorn listinglens.app:app --host 127.0.0.1 --port 8000 --workers 1
```

Open **http://127.0.0.1:8000/**. Stop the service with **Ctrl+C**. If your Python executable is named `python3` and its version is compatible, substitute that for `python3.12`.

### Windows (PowerShell)

```powershell
git clone https://github.com/dito16/listinglens-backend.git
cd listinglens-backend
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn listinglens.app:app --host 127.0.0.1 --port 8000 --workers 1
```

Open **http://127.0.0.1:8000/**. Windows setup has not been independently verified on every system.

### Confirm the server is running

Visit **http://127.0.0.1:8000/health**. A response containing `"status":"ok"` means the application has started; **it does not test upstream access**. API documentation endpoints (`/docs`, `/redoc`, `/openapi.json`) are intentionally disabled.

**No registration, API token, cloud account or paid hosting is required to start the local application.**

## Using the application

1. Open the UI and choose a workflow.
2. For *Collect + Origin Match*, choose a city, deal/property type and filters, then **Check Count** before starting a collection.
3. For *Search by ID*, enter an existing MyHome listing ID.
4. For *PDF Generator*, enter 1–10 distinct IDs separated by commas. Outputs are produced from live source data, not bundled examples.

If a request reports `UPSTREAM_ACCESS_DENIED` or HTTP `401`, `403` or `429`, the **source website declined the request**. Respect that result; do not repeatedly retry or treat missing data as a positive match.

## Project structure

```text
listinglens/
  app.py                FastAPI application and routes
  workflow.py           Collection, matching workflow, job state and exports
  search.py             Query validation and search URLs
  transport.py          Bounded source HTML/SSR fetching
  parser.py             Listing search-page parsing
  detail.py             Listing detail extraction
  matcher.py            Histogram and perceptual-hash matching
  sift_matcher.py       SIFT / RANSAC geometric verification
  pdf_jobs.py           Asynchronous PDF batch processing
  pdf_export/           PDF extraction, evidence handling and rendering
  demo_guard.py         Resource guard for publicly hosted demos
  web/                  HTML/CSS/JavaScript frontend and branding assets
requirements.txt        Python runtime dependencies
.python-version         Recommended Python version
```

The Python Backend is included in this repository; **no source files are deliberately withheld** from the runtime implementation.

## Data, privacy and attribution

- No scraped listing databases, user-submitted files, credentials, personal contact lists, downloadable result archives or generated PDFs are distributed with the source.
- Runtime state and files are created locally under `data/` and are excluded from Git.
- Public real-estate details and photographs belong to their respective owners. **ListingLens is an independent project, not affiliated with or endorsed by MyHome.ge.** Check the source site's terms, applicable laws and image rights before collection, publication or redistribution.
- Map tiles, when available, are supplied by OpenStreetMap contributors and their applicable service policies.
- Public deployment on free cloud platforms may hit request restrictions, CPU, memory, storage or outbound-network limits.

## Source availability and contact

The repository is public so prospective employers and evaluators can **inspect the Python implementation and run it locally**. There is **no published open-source license at present**; redistribution and commercial reuse require permission from the author.

For project or licensing enquiries: **[partners@edgegroup.site](mailto:partners@edgegroup.site)**.
