# Moving the web UI to React and shadcn/ui

Planned with the user on 2026-10-24, not started. Zone rules, heatmaps and dashboards are coming, and those are
tools that keep their state in the browser: drawing zones on a live picture, moving a heatmap through the day,
charts that filter each other. React suits that and htmx doesn't, and the user already uses React in
csmart-digit-desktop. Only the web layer changes: the recognition service, database, CLI and install.bat stay.

## What there is to move (2026-10-24)

- 52 routes in src/portaria/web, 32 templates (about 1,650 lines), style.css (about 870 lines), about 470 lines
  of JavaScript
- 35 web tests (of 141) and 573 translated texts
- About 12 screens:
  - Login, Today, Live
  - People, and a person's page (photos, camera capture, LGPD confirmation)
  - Records (faces and objects)
  - Cameras: list, and the form with connection test, network search, lens split and each model's settings
  - Settings (name and logo, accounts, System, Database) and AI models
  - The door screen

## Decisions

- **Stack:** Vite, TypeScript, React, Tailwind and shadcn/ui, with:
  - React Router for the pages
  - TanStack Query, whose polling replaces htmx's refresh every few seconds
  - react-hook-form for forms
  - Sonner for toasts
  - Lucide icons, the ones the app uses now
- **This app's look, not shadcn's default:** shadcn's CSS variables take today's tokens:
  - orange as primary (#F26B1D, #C64A0E on buttons)
  - the gray canvas and white panels, Instrument Sans, radii 10, 14 and 18 px, dark mode
  - green and red still mean only recognized and not recognized, and toasts stay bottom-right
- **API:** the routes that return pages return JSON instead.
  - Sign-in keeps the session cookie on the same origin, so no CORS.
  - Pictures, live MJPEG streams and downloads stay as they are.
  - A browser still keeps only 6 connections per address, so Today's limit of live videos stays.
- **Serving and install:** FastAPI serves the built app for every path that isn't the API, a picture or a static
  file. Node is only for development, where Vite's dev server sends the API to FastAPI. The built files are
  committed, so install.bat still needs only Python.
- **Translations:** i18next with the same English keys and translation_cache.json, as in csmart-digit-desktop.
  Portuguese stays only in the catalog, curated by hand, and tests/test_i18n.py also checks the React sources.
- **One page at a time:** the app keeps working after every step. While the move runs, new screens go to React
  only, so it doesn't fall behind the other work.

## Step 1: the React app

- [ ] Vite, TypeScript, Tailwind and shadcn/ui project in the repo (folder to choose, such as `ui/`), building
      into the web package's static files
- [ ] The shadcn theme from today's tokens, light and dark, with Instrument Sans and Lucide
- [ ] The app's frame in React: side menu, PT/EN switch, account and sign-out, toasts bottom-right, the default
      password notice
- [ ] Sign-in through the existing session; FastAPI serves the built app
- [ ] i18next reading translation_cache.json; test_i18n.py extended to the React sources
- [ ] Browser tests (Playwright) set up, beside API tests in pytest

## Step 2: the new tools first

- [ ] Zone editor: draw and edit zones on a camera's picture, for the zone rules
- [ ] Heatmaps: the recognition service saves photos of what it finds but not yet where people are over time, so
      it adds that up per camera (for example a grid per hour in SQLite); the browser draws it over the picture
      and moves through the day
- [ ] Dashboards: totals from database queries, drawn with shadcn charts

## Step 3: the existing pages, simplest first

Each page gets its JSON endpoints and tests, and its template is deleted when it moves.

- [ ] Settings (name and logo, accounts, System, Database) and AI models
- [ ] People and a person's page (photos, camera capture, LGPD confirmation)
- [ ] Records (faces and objects, filters, downloads)
- [ ] Cameras (list, form, connection test, network search, lens split, each model's settings)
- [ ] Live
- [ ] Today (camera list beside the menu, cards as tall as the picture, refresh every 5 s)
- [ ] The door screen (full screen, its secret key, the green and red decisions)

## Step 4: finish

- [ ] Remove htmx, the Jinja templates and the old style.css
- [ ] Update README.md and the connect-jortan-camera skill (screens and labels)

## Effort and risks

- Roughly 2 to 4 weeks of focused work to reach today's features (estimate from 2026-10-24).
- Most of the risk is in the details: form errors, the LGPD confirmation, admin-only parts, actions that only
  work on the server computer (Open folder), the door screen's behavior, and toasts that hide or stay.
