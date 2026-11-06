# RuneChat
A chat application

## GitHub Pages demo

The static demo is published from [`docs/`](docs/) by the GitHub Actions workflow when changes are pushed to `main`. In the repository settings, set **Pages > Build and deployment > Source** to **GitHub Actions**. The first deployment will then be available at:

https://fsminecrafter.github.io/RuneChat/

The Pages version is a browser-local demo: conversations are saved in that browser's local storage and are not shared between visitors or devices. The full multi-user chat app remains the Flask and Socket.IO application in `main.py`; GitHub Pages does not run its Python backend.
