# AutoPrint

AutoPrint prints a colorful maintenance page on a weekly schedule to help reduce printhead nozzle clogging on inkjet printers.

This project is built for Docker + CUPS and works well with IPP Everywhere printers (including Canon GX series).

## Features

- Weekly scheduled maintenance print
- Manual print trigger from a small web UI
- Printer reachability check via IPP (`ipptool`)
- CUPS integration for network printer queues
- Containerized setup with Docker Compose
- Optional automatic image publishing to GHCR and Docker Hub (GitHub Actions)

## Quick Start

1. Copy `.env.example` to `.env`
2. Set your printer values in `.env`
3. Start:

```bash
docker compose up -d
```

4. Open:
- Dashboard: `http://localhost:8080`
- CUPS UI: `http://localhost:631`

## Configuration

Environment variables (set in `.env`):

| Variable | Required | Example | Description |
|---|---|---|---|
| `PRINTER_URI` | yes | `ipp://my-printer.local/ipp/print` | IPP URI of your printer |
| `PRINT_WEEKDAY` | yes | `monday` | Print day (`monday`..`sunday`) |
| `PRINT_TIME` | yes | `10:00` | Print time in 24h format |
| `TZ` | yes | `Europe/Berlin` | Timezone |
| `PRINTER_NAME` | no | `AutoPrinter` | CUPS queue/display name |
| `DNS_SERVER` | no | `192.168.0.1` | Optional DNS server for container hostname resolution |

## Using the Published Image

No build required. Pull directly from the registry and run.

### docker run

```bash
docker run -d \
  --name autoprint \
  --restart unless-stopped \
  -p 8080:8080 \
  -p 631:631 \
  --env-file .env \
  ahemoh/autoprint:latest
```

Or from GHCR:

```bash
docker run -d \
  --name autoprint \
  --restart unless-stopped \
  -p 8080:8080 \
  -p 631:631 \
  --env-file .env \
  ghcr.io/ahemoh/autoprint:latest
```

### docker compose (no local build)

Replace `build: .` with the `image:` line in `docker-compose.yml`:

```yaml
services:
  autoprint:
    image: ahemoh/autoprint:latest   # pull from Docker Hub
    # image: ghcr.io/ahemoh/autoprint:latest  # or from GHCR
    container_name: autoprint
    restart: unless-stopped
    env_file: .env
    ports:
      - "8080:8080"
      - "631:631"
    volumes:
      - autoprint_data:/data

volumes:
  autoprint_data:
```

Then start with:

```bash
docker compose up -d
```

## Build and Run Locally

```bash
docker build -t autoprint:local .
docker run --rm -p 8080:8080 -p 631:631 --env-file .env autoprint:local
```

## Publish Images Manually

### Docker Hub

```bash
docker build -t autoprint:local .
docker tag autoprint:local <dockerhub-user>/autoprint:latest
docker login
docker push <dockerhub-user>/autoprint:latest
```

### GHCR

```bash
docker build -t autoprint:local .
docker tag autoprint:local ghcr.io/<github-user>/autoprint:latest
docker login ghcr.io -u <github-user>
docker push ghcr.io/<github-user>/autoprint:latest
```

## Automatic Publish (GitHub Actions)

Workflow file: `.github/workflows/docker-publish.yml`

- Publishes to GHCR on push to `main`, tags (`v*.*.*`), or manual run.
- Publishes to Docker Hub only if both secrets are set:
  - `DOCKERHUB_USERNAME`
  - `DOCKERHUB_TOKEN`

## Security Note

Do not commit `.env`.
It may contain private network details (hostnames, printer URI, local DNS).

## License

Licensed under Apache License 2.0. See `LICENSE`.
