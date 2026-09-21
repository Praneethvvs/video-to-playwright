# testboard, for running beside the application under test.
#
# NOT YET BUILT OR DEPLOYED. Treat the first build as debugging rather than as a release.
#
# The image is deliberately thin: the same package the local installer uses, plus the browsers.
# There is no second codebase for "the deployed version", so a fix ships to a laptop and to the
# cluster as the same version number.
#
# The test repository is NOT baked in. It is mounted at /repo, which keeps the image independent
# of any one team's tests and means the same image serves every repo.

FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

# The Playwright base image already carries the browsers and their system libraries. Installing
# them here instead is the single most common reason a container "works locally" and then cannot
# start a browser in the cluster.

WORKDIR /opt/testboard
COPY pyproject.toml ./
COPY testboard ./testboard
# [codex] pulls the Codex SDK and its pinned CLI binary, so turning a recording into tests needs
# nothing installed at runtime. Drop the extra if this deployment will never generate.
RUN python -m pip install --no-cache-dir ".[codex]"

# The skill travels in the image rather than being fetched at runtime, so the agent has the
# workflow it is meant to follow even in a pod with no egress beyond the model API.
#   docker build -f testboard/Dockerfile --build-context skill=. -t testboard:0.1.0 .
COPY --from=skill . /opt/skills/video-to-playwright
ENV TESTBOARD_SKILL_PATH=/opt/skills/video-to-playwright

# testboard's own environment. The repository under test keeps its own virtualenv at
# /repo/venv — they must not be the same one, or the web framework and the test suite start
# negotiating dependency versions with each other.
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1

# Behind a TLS-intercepting proxy, mount the corporate root and point this at it. The Codex
# runtime is Rust and verifies against its own compiled-in roots, so without it every model call
# fails as "invalid peer certificate: UnknownIssuer" — which reads like a network fault and is
# not one. On a laptop testboard exports the OS trust store itself; in a container there is no
# OS store to export, so this is the seam.
# ENV SSL_CERT_FILE=/etc/ssl/corporate/ca-bundle.pem

WORKDIR /repo
EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8770/healthz', timeout=2).status==200 else 1)"

# 0.0.0.0 inside the container is fine; what must not be skipped is the ingress-level
# authentication in front of it. This process starts runs that mutate a shared environment.
CMD ["python", "-m", "testboard", "--repo", "/repo", "--host", "0.0.0.0", "--port", "8770"]
