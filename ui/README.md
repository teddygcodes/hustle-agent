# Glint Dashboard UI

React + TypeScript + Vite dashboard for the Glint trading bot.

See the [root README](../README.md#dashboard-ui) for full documentation.

## Quick Start

```bash
cd ui
npm install
npm run dev
```

Starts the Express API server (port 3001) and Vite dev server (port 5173).
The UI polls `../bot/state/` every 5 seconds for live data.
