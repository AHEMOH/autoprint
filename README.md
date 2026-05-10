# AutoPrint

AutoPrint prints a colorful maintenance page on a weekly schedule to reduce inkjet nozzle clogging.

It is designed for Docker + CUPS and works best with IPP Everywhere printers.

## What you get

- Weekly scheduled maintenance print
- Manual print trigger from web UI
- Bring-your-own images (PDF/PNG/JPG) — kid coloring pages, scanned drawings, anything you have a right to print
- Round-robin through your images so nothing repeats unnecessarily
- A slim color strip is always appended to keep every ink nozzle in use, regardless of the image
- IPP reachability check before each print
- CUPS queue handling inside the container
- Ready-to-use images on Docker Hub and GHCR

## Quick start (recommended: published image)

Use this `docker-compose.yml` template and replace placeholders.

```yaml
services:
  autoprint:
    image: <registry-user>/autoprint:latest
    # Example Docker Hub: ahemoh/autoprint:latest
    # Example GHCR: ghcr.io/<github-user>/autoprint:latest
    container_name: autoprint
    restart: unless-stopped
    environment:
      PRINTER_URI: 'ipp://<printer-host-or-ip>/ipp/print'
      PRINT_WEEKDAY: 'monday'
      PRINT_TIME: '08:00'
      TZ: 'Europe/Berlin'
      PRINTER_NAME: 'MyPrinter'
    ports:
      - "8080:8080"
      - "631:631"
    volumes:
      - autoprint_data:/data

volumes:
  autoprint_data:
```

Start:

```bash
docker compose up -d
```

Open:

- Dashboard: `http://localhost:8080`
- CUPS UI: `http://localhost:631`

## Environment variables

| Variable | Required | Example | Description |
|---|---|---|---|
| `PRINTER_URI` | yes | `ipp://printer.local/ipp/print` | Printer IPP endpoint |
| `PRINT_WEEKDAY` | yes | `monday` | `monday` to `sunday` |
| `PRINT_TIME` | yes | `08:00` | 24h format |
| `TZ` | yes | `Europe/Berlin` | Container timezone |
| `PRINTER_NAME` | no | `MyPrinter` | CUPS queue name |
| `MAINTENANCE_STRIP` | no | `on` | Append color strip to user images (`on`/`off`) |
| `GUNICORN_WORKERS` | no | `1` | Gunicorn worker processes |
| `GUNICORN_THREADS` | no | `4` | Threads per worker |
| `GUNICORN_TIMEOUT` | no | `60` | Request timeout (seconds) |

## docker run (alternative)

```bash
docker run -d \
  --name autoprint \
  --restart unless-stopped \
  -p 8080:8080 \
  -p 631:631 \
  -e PRINTER_URI='ipp://<printer-host-or-ip>/ipp/print' \
  -e PRINT_WEEKDAY='monday' \
  -e PRINT_TIME='08:00' \
  -e TZ='Europe/Berlin' \
  -e PRINTER_NAME='MyPrinter' \
  -v autoprint_data:/data \
  <registry-user>/autoprint:latest
```

## Bring your own images

Drop `.pdf`, `.png`, `.jpg`, or `.jpeg` files into the dashboard's **Images** card
(or directly into the `autoprint_data` volume under `/data/images/`).
AutoPrint then prints them on the weekly schedule, round-robin (oldest-printed first),
instead of the synthetic color test page. An empty folder always falls back to the test page.

A slim color **maintenance strip** is automatically composited onto the bottom of
every printed user image (or appended as an extra page for PDFs). This keeps every
ink nozzle — especially yellow — in regular use even when the image itself is
mostly one color. Disable with `MAINTENANCE_STRIP=off` if you really do not want it.

Where to get printable images legally:

- Official free coloring pages from the IP owner — e.g. Spin Master / Nickelodeon
  for *Paw Patrol*, Disney's "Family" site for *Frozen*, etc.
- Public-domain coloring sources like [openclipart.org](https://openclipart.org)
- Scans of your kid's own drawings

Do not redistribute copyrighted material — printing for personal/family use is
typically fine; bundling those files into a public Docker image is not.

## Notes

- The container uses Gunicorn (WSGI) for production web serving.
- If `.local` or `.home.arpa` names do not resolve inside Docker, use the printer IP in `PRINTER_URI`.
- Do not publish private network values in public repos.

## License

Apache License 2.0. See `LICENSE`.
