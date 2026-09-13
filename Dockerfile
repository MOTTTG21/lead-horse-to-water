# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

# Copy only what's needed to resolve dependencies first, so dependency
# installs are cached across builds that only change application code.
COPY pyproject.toml ./
COPY src ./src

# A real (non-editable) install -- this is what makes the package-data
# declaration in pyproject.toml load-bearing: templates/static must be
# bundled, not just present in the source tree next to an editable link.
RUN pip install --no-cache-dir .

# Secrets (ANTHROPIC_API_KEY, Auth0 client secret) are injected as
# environment variables at deploy time -- from GCP Secret Manager in
# production, never baked into the image. See docs/design-plan.md.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn horse_gateway.web.server:app --host 0.0.0.0 --port ${PORT}"]
