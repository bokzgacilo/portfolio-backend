# Background Remover API

A single-purpose FastAPI service: POST an image, get a transparent PNG cutout
back. It powers `/tools/image/background-remover` on the Next.js frontend.

Background removal is [rembg](https://github.com/danielgatis/rembg) running a
U^2-Net ONNX model on CPU. Everything else in `main.py` is the guard rail
around it: content-type checks, a size cap, EXIF orientation, and a downscale
pass so one oversized upload cannot exhaust a small instance.

## Endpoints

| Method | Path                     | Returns                                        |
| ------ | ------------------------ | ---------------------------------------------- |
| `GET`  | `/`                      | Service info and current limits                |
| `GET`  | `/health`                | `{"status":"ok","model":...,"ready":true}`     |
| `POST` | `/api/remove-background` | `image/png` (RGBA) — the cutout                |
| `GET`  | `/docs`                  | Interactive OpenAPI docs (FastAPI built-in)    |

`POST /api/remove-background` takes `multipart/form-data` with the image under
the field name **`file`**. JPG, PNG, and WebP are accepted.

On success the response carries a receipt in its headers, all of them exposed
to browser JavaScript through CORS:

```
X-Processing-Ms     inference time, milliseconds
X-Model             model that produced the cutout
X-Original-Width    dimensions of the uploaded image
X-Original-Height
X-Output-Width      dimensions of the returned PNG
X-Output-Height
X-Downscaled        "1" when the input exceeded MAX_DIMENSION
```

Every failure — wrong type, empty body, too large, unreadable image, model
error — answers with the same shape, so the client has one thing to read:

```json
{ "error": "Unsupported type: text/plain. Send a JPG, PNG, or WebP." }
```

Status codes: `400` unreadable/empty/malformed, `413` too large, `415`
unsupported type, `500` model failure, `503` model still loading.

## Configuration

Every variable has a working default; none are required.

| Variable              | Default                                | Notes                                                    |
| --------------------- | -------------------------------------- | -------------------------------------------------------- |
| `REMBG_MODEL`         | `u2netp`                               | `isnet-general-use` or `u2net` for better edges (more RAM) |
| `MAX_UPLOAD_BYTES`    | `10485760` (10 MB)                     | Rejected with `413` above this                            |
| `MAX_DIMENSION`       | `2000`                                 | Longest edge; larger inputs are downscaled first          |
| `ALLOWED_ORIGINS`     | production domains only                 | Comma-separated                                          |
| `ALLOWED_ORIGIN_REGEX`| empty                                   | Optional regex for non-production preview origins         |
| `STRICT_ORIGIN_CHECK` | `1`                                     | Rejects `/api/remove-background` when `Origin` is not allowed |
| `REMBG_HOME`          | `~/.rembg`                             | Where model files are cached                              |

## Run locally

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload            # http://127.0.0.1:8000
```

The model downloads on first startup (~5 MB for `u2netp`), then comes off disk.

Smoke test:

```bash
curl -X POST -F "file=@photo.jpg" \
  http://127.0.0.1:8000/api/remove-background --output cutout.png
```

Then point the frontend at it — in `frontend/.env`:

```
NEXT_PUBLIC_BACKGROUND_REMOVER_API=http://127.0.0.1:8000
```

## Deploy on Render

The frontend repo's root *is* the Next app, so this directory needs a repo of
its own. Push it, then let the blueprint do the rest:

```bash
cd backend
git init && git add . && git commit -m "feat: background removal API"
gh repo create portfolio-backend --private --source=. --push
```

In Render: **New → Blueprint**, pick that repo. `render.yaml` supplies the
build and start commands, the health check, and the env vars. Delete the
`rootDir: backend` line if the backend is the repo root (it is, if you pushed
this directory on its own) and adjust `REMBG_HOME` to
`/opt/render/project/src/.rembg` to match.

No blueprint? Create a Python **Web Service** by hand with:

- Build: `pip install -r requirements.txt && python -c "from rembg import new_session; new_session('u2netp')"`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1`
- Health check path: `/health`

Once it is live, set `ALLOWED_ORIGINS` to the production domains and add the
service URL to Vercel as `NEXT_PUBLIC_BACKGROUND_REMOVER_API`.

### Free-tier realities

- **Cold starts.** A free instance sleeps after ~15 minutes idle and takes
  ~30–60 s to wake. The frontend pings `/health` when the tool page mounts so
  that wait usually overlaps with the visitor choosing a file, and it says
  "waking the server" if a request lands during one.
- **512 MB RAM.** Hence `u2netp` (~5 MB), one worker, and the 2000 px
  downscale. `u2net` (~176 MB) and the BiRefNet models want a paid instance.
- **Ephemeral disk.** Model files are re-downloaded on each deploy, which is
  why the build command warms the cache instead of the first request.

## Deploy updates on the Bluehost VPS

The backend is checked out at `/opt/api`. After the deployment script has been
committed and pushed to the backend repository, get it once on the VPS:

```bash
cd /opt/api
git pull --ff-only
```

Then deploy with one command, from any directory:

```bash
bash /opt/api/deploy.sh
```

The script pulls the current branch's upstream with `--ff-only`, installs pinned
requirements into the existing virtualenv, checks dependency consistency and
Python syntax, restarts the backend, waits for local model readiness, restarts
`cloudflared`, and waits for the public HTTPS health endpoint to report
`status: ok` and `ready: true`. A deployment lock prevents concurrent runs.

It restarts `bok-api.service`, the backend service on this VPS.
If your service name changes or your virtualenv lives elsewhere, configure once:

```bash
cp /opt/api/.deploy.conf.example /opt/api/.deploy.conf
nano /opt/api/.deploy.conf
```

Set `API_SERVICE` to the backend's service name and `PYTHON_BIN` to the exact
virtualenv Python used by that service. `.deploy.conf` is ignored by Git.
No API or Cloudflare token is needed: the existing services retain their settings.
Run as the repository owner with sudo access, or as root if root owns this checkout.

The script refuses tracked local edits, detached HEADs, missing upstreams, missing
services, and system Python. If dependencies fail, it does not restart services.
If local readiness fails, it does not restart the tunnel. Later failures exit
nonzero and print diagnostic commands. Pulling and installing modify the checkout
and environment in place; failures do not automatically roll them back. Restarting
can interrupt in-flight requests, so use a quiet moment for updates.

This is a manually triggered deployment command, not a push-triggered CI pipeline.
It does not upgrade the VPS operating system or the `cloudflared` binary.
