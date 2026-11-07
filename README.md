# AutoPrint

AutoPrint prints one colorful page per week so the printhead nozzles of an inkjet
printer (Canon GX2050 / MegaTank in particular) do not clog. Yellow is usually the
first channel to go, so yellow gets extra attention.

It runs as a single Docker container with CUPS + IPP Everywhere, so no vendor driver
is needed. A small web dashboard (German) lets you manage images and print settings.

## What you get

- Weekly scheduled maintenance print plus a "print now" button
- **Maintenance color strip on every page** – a CMYK strip with triple yellow and a
  yellow density ramp. By default it sits at the **top** of the page, so the nozzles
  are flushed *before* the actual picture is printed and the picture comes out clean.
- **Photo quality on demand** – choose print quality and paper mode (plain / photo)
  in the dashboard. Photo mode makes the printer lay down noticeably more ink.
  The paper list is read from the printer's own IPP capabilities.
- **Image pool** – upload several PNG/JPG/PDF files at once (drag & drop), or import
  a file from a URL. Thumbnails, per-image print / preview / delete.
- Round-robin (longest-unprinted first) or random selection
- Synthetic color test page as fallback when the pool is empty
- Preview of the exact page that will be printed
- Settings are changed in the dashboard and persist in the data volume, no restart needed
- IPP reachability check before each print, history of the last 20 jobs
- Light and dark theme, works on a phone, no external assets (fine without internet)
- Ready-to-use images on Docker Hub and GHCR

## Quick start (recommended: published image)

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

```bash
docker compose up -d
```

- Dashboard: `http://localhost:8080`
- CUPS UI: `http://localhost:631`
- JSON status: `http://localhost:8080/status`

## Environment variables

| Variable | Required | Example | Description |
|---|---|---|---|
| `PRINTER_URI` | yes | `ipp://printer.local/ipp/print` | Printer IPP endpoint |
| `PRINT_WEEKDAY` | yes | `monday` | `monday` to `sunday` (lowercase English) |
| `PRINT_TIME` | yes | `08:00` | 24h format |
| `TZ` | yes | `Europe/Berlin` | Container timezone |
| `PRINTER_NAME` | no | `MyPrinter` | CUPS queue name |
| `PRINT_QUALITY` | no | `5` | Initial IPP print-quality: `4` normal, `5` high |
| `PRINT_MEDIA_TYPE` | no | `stationery` | Initial IPP media-type keyword (`photographic-glossy` = photo mode) |
| `PRINT_CONTENT_OPTIMIZE` | no | `photo` | IPP print-content-optimize (`auto`/`photo`), sent only if supported |
| `MAINTENANCE_STRIP` | no | `on` | Initial strip on/off |
| `STRIP_POSITION` | no | `top` | Initial strip position `top` / `bottom` |
| `IMAGE_ORDER` | no | `roundrobin` | Initial selection `roundrobin` / `random` |
| `SECRET_KEY` | no | random | Flask secret for dashboard flash messages |
| `GUNICORN_WORKERS` | no | `1` | Keep at 1 (the scheduler runs per process) |
| `GUNICORN_THREADS` | no | `4` | Threads per worker |
| `GUNICORN_TIMEOUT` | no | `60` | Request timeout (seconds) |

The print settings (`PRINT_QUALITY`, `PRINT_MEDIA_TYPE`, `MAINTENANCE_STRIP`,
`STRIP_POSITION`, `IMAGE_ORDER`) are **initial values only**. Once changed in the
dashboard they are stored in `/data/settings.json`, which takes precedence. Delete
that file to go back to the environment values.

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

## Photo quality and nozzle flushing

`print-quality=5` (IPP "high") is the default. The real "photo mode" of a Canon
inkjet is selected through the **paper type**: choosing a photographic media type
(e.g. `photographic-glossy`) makes the printer use its photo ink laydown, which puts
much more ink through every nozzle – good for flushing, but the sheet gets wetter on
plain paper. The dashboard's paper dropdown lists exactly the `media-type-supported`
keywords your printer reports (cached in `/data/printer_caps.json`).

The maintenance strip is printed **first** (top of the page, or as a leading page for
PDFs) so that cyan, magenta, yellow and black are all flowing before the picture
itself is printed. Switch it to the bottom or off in the dashboard if you prefer.

To see what your printer advertises:

```bash
docker exec autoprint ipptool -tv "$PRINTER_URI" get-printer-attributes.test \
  | grep -E 'media-type-supported|print-quality-supported|print-content-optimize-supported'
```

## Bring your own images (kids' coloring pages etc.)

Upload `.pdf`, `.png`, `.jpg` or `.jpeg` files in the dashboard (several at once,
drag & drop) or paste a direct link to a file and click "Von Link holen". Files land in
the `autoprint_data` volume under `/data/images/`. AutoPrint then prints them on the
weekly schedule instead of the synthetic test page; an empty pool always falls back
to the test page.

AutoPrint deliberately does **not** search the internet for pictures of Paw Patrol,
Peppa Pig and friends: those characters are copyrighted, there is no official API,
and nothing guarantees that a random hit is suitable for children. Collect a few
pages in advance instead:

- Official free coloring pages from the rights holders (Nick Jr. activity pages for
  Paw Patrol, peppapig.com, the official Masha and the Bear site, Disney "Family", …)
- Public-domain graphics, e.g. [openclipart.org](https://openclipart.org) or Wikimedia Commons
- Scans or photos of your kids' own drawings
- Colorful photos – the more color, the better for the nozzles

Printing for personal/family use is fine; do not redistribute copyrighted files or
bundle them into a public Docker image.

## Notes

- The container uses Gunicorn (WSGI) for production web serving.
- If `.local` or `.home.arpa` names do not resolve inside Docker, use the printer IP in
  `PRINTER_URI`, or set `dns:` / `extra_hosts:` in `docker-compose.yml`.
- PDF thumbnails and previews use poppler (`pdftoppm`), included in the image.
- Do not publish private network values in public repos.

## License

Apache License 2.0. See `LICENSE`.
