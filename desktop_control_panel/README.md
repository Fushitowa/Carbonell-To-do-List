# StackPilot desktop control panel

A CustomTkinter desktop app for starting, stopping, restarting, and monitoring this repository's local full stack. It starts the services concurrently, captures both output streams, shows PIDs and live status, and stops the PostgreSQL container and tracked process trees when services or the app are stopped.

## Requirements

- Python 3.10 or newer
- Node.js and npm
- Docker Desktop with Docker Compose

Python packages, including the panel and API services, are listed in `requirements.txt`: `customtkinter`, `psutil`, `fastapi`, `uvicorn`, `httpx`, and `psycopg`. On Linux, install the system Tk package as well (commonly `python3-tk`).

## Install and run

From this directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

On macOS/Linux, use `python3 -m venv .venv`, activate with `source .venv/bin/activate`, install requirements, and run `python app.py`.

## Start the stack

Install and start Docker Desktop before using **Run All**. The panel then starts the Compose PostgreSQL service, waits for the FastAPI backend to connect to it, starts the API gateway, and launches Vite. The UI waits up to about 100 seconds for the local API before it falls back to browser storage.

Use **Open To-do List** to start any stopped services, wait for Vite to respond, and open the app in your default browser automatically.

The stack uses these local endpoints:

- PostgreSQL: `127.0.0.1:5432`
- Backend: `http://127.0.0.1:8000` (`/health` and `/tasks`)
- API gateway: `http://127.0.0.1:8001` (`/health` and `/api/tasks`)
- Frontend: `http://127.0.0.1:5173`

Task data is stored in the local PostgreSQL volume `daylist_postgres_data`. Use Docker Desktop to back up or remove this development database. Stop the Database card to stop the container; the named volume keeps tasks across restarts.

By default the frontend uses the local API. If `.env.local` sets both `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`, it continues using Supabase instead; remove those settings to use local PostgreSQL.

Use **Settings** in the app to edit `services.json`. The commands use `{python}` to run the API services with the same Python virtual environment as the control panel. Each card has Start, Stop, and Restart controls; the console can show all services or filter to one. Closing the panel stops the database container and managed process trees.
