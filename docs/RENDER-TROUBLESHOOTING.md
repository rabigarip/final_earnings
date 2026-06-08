# Render troubleshooting

## "Report still shows old format" after deploy

1. **Clear build cache and redeploy**
   - In Render: open your service → **Settings** → under **Build & Deploy**, click **Clear build cache & deploy** (or **Manual Deploy** → **Clear build cache & deploy**).
   - This forces a clean install so the running container has the latest code.

2. **Create a new report, don't reopen an old one**
   - Each report is generated once; the .docx file is not regenerated.
   - Click **New Report** → enter ticker (e.g. 2222.SR) → **Generate Report**.
   - When it finishes, open **that** report (the one that just appeared at the top).
   - Old reports in the list were built with whatever code was live at the time.

3. **Avoid browser cache**
   - Use **Download** (with cache-busting) or try an incognito/private window.
   - The download endpoint sends `Cache-Control: no-store` so the browser shouldn't cache the file.

4. **Confirm deploy**
   - Open `https://<your-service>.onrender.com/health` — you should see `{"status":"ok","version":"0.1.0"}`.
   - In a newly generated memo, look for the line **"Generated with earnings-research v0.1.0"** under the expected report date. If that line is there, the report was built by the current app version.
