# One site on Render (API + frontend in one URL)

One Render **Web Service** serves both the API and the app UI at the same URL (e.g. `https://earnings-research.onrender.com`). No separate Static Site.

## How it works

- The **frontend** lives in `frontend/` and is built into `static/` (HTML, JS, CSS).
- The **FastAPI** app serves `/api/*` as before and, when `static/index.html` exists, serves the UI at `/` and other paths (SPA fallback).
- Render only runs Python; it does not run Node. So you **build the frontend on your machine** and **commit the `static/` folder**. Render’s build is just `pip install -r requirements.txt`.

## Steps

### 1. Build the frontend into `static/`

From the repo root (with Node/npm installed):

```bash
./scripts/build_static.sh
```

Or by hand:

```bash
cd frontend
npm install
VITE_API_BASE_URL= npm run build
cp -r dist ../static
cd ..
```

`VITE_API_BASE_URL=` (empty) makes the app call the same origin (`/api/...`), so one URL works.

### 2. Commit and push

```bash
git add static/
git commit -m "Update static frontend for one-site deploy"
git push origin main
```

### 3. Deploy on Render

- **One** Web Service, repo = **earnings-research**.
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `uvicorn src.api:app --host 0.0.0.0 --port $PORT`

After deploy, open `https://<your-service>.onrender.com` — you get the login page and app. API docs: `https://<your-service>.onrender.com/docs`.

### 4. When you change the frontend

Run `./scripts/build_static.sh` again, then commit and push `static/`.

## Login

The backend has a **stub** `POST /api/auth/login` that accepts any access code so the login screen works. Replace with real auth if you need it.
