"""Admin-triggered application update for the Docker deployment.

A separate container: the application cannot replace its own container, so
this one does it on the application's behalf. It owns the two things the app
must never have — the Docker socket and write access to the git checkouts —
and accepts commands only from the backend (shared token). See
docs/deployment-docker.md, «Оновлення з адмінки».
"""
