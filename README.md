# RuneChat
A chat application

## GitHub Pages demo

The static demo is published from [`docs/`](docs/) by the GitHub Actions workflow when changes are pushed to `main`. In the repository settings, set **Pages > Build and deployment > Source** to **GitHub Actions**. The first deployment will then be available at:

https://fsminecrafter.github.io/RuneChat/

The Pages version is a browser-local demo: conversations are saved in that browser's local storage and are not shared between visitors or devices. The full multi-user chat app remains the Flask and Socket.IO application in `main.py`; GitHub Pages does not run its Python backend.

## Render deployment

`render.yaml` configures the Flask-SocketIO backend to run under Gunicorn with one threaded worker. For an existing Render service, set its **Start Command** to:

```sh
gunicorn --worker-class gthread --threads 100 --workers 1 --bind 0.0.0.0:$PORT main:app
```

Ensure the service installs dependencies with `pip install -r requirements.txt`, then redeploy. The free service uses an ephemeral filesystem, so SQLite data and uploaded files are not durable across restarts or deploys.
