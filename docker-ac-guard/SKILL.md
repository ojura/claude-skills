---
name: docker-ac-guard
description: Run named foreground Docker build and validation jobs only on AC, with owned container cleanup and cancellation handling. Use when a runner accepts a Docker-command override and must not signal its parent or a live service.
---

Read [README.md](README.md) before configuring the runner. Use the maintained
`scripts/docker_ac_guard.py` executable directly as its Docker-command override.
The executable must be the PID the runner tracks and waits for.

Confirm authorization for the calling operation separately. This skill does not
authorize a deployment, live-service restart, credential change or unrelated
container cleanup. Keep the runner's documented image, networks, resource limits
and configuration settings.

Check physical AC immediately before real heavy execution. Keep a private
`DOCKER_AC_LOG_DIR`, inspect the completed power and cleanup results, and retain
actual failures and skipped controls. A failed Docker query is not evidence that
a container has stopped. The guard can wait indefinitely for Docker to confirm
cleanup; do not kill its parent or a live service to bypass that wait.

Run the isolated protocol tests before relying on a changed guard. Real-container
controls require explicit opt-in and use only owned, network-isolated containers.
