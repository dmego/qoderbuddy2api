FROM python:3.12-slim

WORKDIR /app

# Copy everything needed by the build
COPY README.md pyproject.toml ./
COPY src/ src/
COPY config/ config/

# Install the package
RUN pip install --no-cache-dir .

ENV QB2API_HOST=0.0.0.0
ENV QB2API_PORT=9999

EXPOSE 9999

CMD ["python", "-m", "qb2api"]
