# Frontend

React + TypeScript status page for the AFLUX Invoice Breakdown Agent. It displays API connectivity. The headless workflow runs through the backend. This scaffold deploys to Azure Static Web Apps via `.github/workflows/frontend-cd.yml`.

## Stack

- React 19 + TypeScript
- Vite 6
- Tailwind CSS 4 (`@tailwindcss/vite`)
- React Router
- ESLint + TypeScript ESLint
- Lightweight shadcn-style `Button`

Requires Node.js 20+.

## Scripts

| Command | Purpose |
| --- | --- |
| `npm run dev` | Local Vite dev server (`http://localhost:5173`) |
| `npm run lint` | ESLint |
| `npm run typecheck` | `tsc -b` |
| `npm run build` | Typecheck + production build → `dist/` |
| `npm run preview` | Preview production build |

## Setup

```bash
cd frontend
cp .env.example .env
npm install
npm run dev
```

Set `VITE_API_BASE_URL` to the FastAPI origin. GitHub Actions uses repository variable `VITE_API_BASE_URL` for demo builds.

## Notes

- Path alias: `@/*` → `src/*`
- SWA SPA fallback: `public/staticwebapp.config.json` (copied into `dist/`)
- Keep secrets out of the browser; only public API URLs belong in `VITE_*`
