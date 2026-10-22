# ArbRadar

Live sportsbook odds monitoring and arbitrage dashboard. The dashboard and collector API run as one service; collector reports are written under `reports/`.

## Cloud deployment

The repository-root `render.yaml` defines an always-on Render web service. It builds the application from this directory, installs Playwright Chromium, serves the dashboard from the API origin, and attaches persistent storage for reports and opportunity history. Deploy the Blueprint from the repository's Render dashboard after connecting the GitHub repository. The service and persistent disk use paid Render resources.

## Local development

Install Node.js and Python dependencies, then run the Vite frontend and FastAPI backend. The Vite development server proxies `/engine` requests to `http://127.0.0.1:8001`.
