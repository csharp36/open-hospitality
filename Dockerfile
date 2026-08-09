# The cloud demo image (Pillar K1): ONE image, two entrypoints.
#   scripts/cloud/entrypoint.sh  -> the serving container (API + SPA + loopback mocks)
#   scripts/cloud/job.sh         -> the migrate-seed job (mocks -> alembic -> demo seed)
# The demo world is fictitious by construction: the ONLY roster this image
# carries is scripts/cloud/demo_roster.json (invented people, 9000+ refs).
# .dockerignore keeps the encrypted-volume artifacts (.dev, punch-photos,
# .env, .demo-kiosk-token) out of the build context — pinned by test.

########## 1. frontend build ##########
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# The deployed issuer is a build-time swap (frontend/src/auth/oidc.ts);
# defaults speak the K3/K5 public hostnames.
ARG VITE_OIDC_AUTHORITY=https://auth.example.com/realms/usali
ARG VITE_OIDC_CLIENT_ID=operator-portal
ENV VITE_OIDC_AUTHORITY=${VITE_OIDC_AUTHORITY} \
    VITE_OIDC_CLIENT_ID=${VITE_OIDC_CLIENT_ID}
RUN npm run build

########## 2. python deps + face models ##########
FROM python:3.13-slim AS python-deps
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /uvx /usr/local/bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra face --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra face
# Models are fetched sha256-pinned (F2's rule: what runs is what was
# reviewed) into the image at build — zero startup moving parts.
COPY scripts/fetch_face_models.py scripts/fetch_face_models.py
RUN /app/.venv/bin/python scripts/fetch_face_models.py /app/models/face

########## 3. runtime ##########
FROM python:3.13-slim
RUN useradd --system --create-home --uid 1001 usali
WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" \
    USALI_FACE_MODEL_DIR=/app/models/face \
    PORT=8080

COPY --from=python-deps /app/.venv /app/.venv
COPY --from=python-deps /app/models/face /app/models/face
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY mapping ./mapping
COPY scripts/demo_seed.py scripts/demo_seed.py
COPY scripts/cloud ./scripts/cloud
# The seed's fixtures: synthetic star faces only. The sample revenue
# PDFs are deliberately NOT copied — the cloud seed is always
# --synthetic-year (job.sh), so `_seed_documents` never runs here, and
# those PDFs carry REAL production figures (K6b/K7): they must not ride
# in a public image, even unreferenced.
COPY tests/fixtures/faces ./tests/fixtures/faces
COPY --from=frontend /build/dist ./frontend/dist

# The seed writes the kiosk device tokens to REPO_ROOT (= /app); /app
# stays root-owned (immutable app dir), so pre-create the one writable
# file instead of chowning the tree.
RUN touch /app/.demo-kiosk-token && chown usali /app/.demo-kiosk-token

USER usali
EXPOSE 8080
ENTRYPOINT ["/app/scripts/cloud/entrypoint.sh"]
