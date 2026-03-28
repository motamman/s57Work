# s57-to-mbtiles — Install Guide

Converts S-57 ENC charts (.000) to vector MBTiles for SignalK / Freeboard-SK.

Requires two things: **tippecanoe** (native) and **podman or docker** (for GDAL).

---

## Raspberry Pi (Debian/Ubuntu, ARM64)

### Tippecanoe (build from source)

```bash
sudo apt update
sudo apt install -y build-essential libsqlite3-dev zlib1g-dev git
cd /tmp
git clone https://github.com/felt/tippecanoe.git
cd tippecanoe
make -j4
sudo make install
tippecanoe --version
```

### Podman

```bash
sudo apt install -y podman
```

### Pull the GDAL image (one time)

```bash
podman pull ghcr.io/osgeo/gdal:alpine-small-latest
```

---

## macOS

### Tippecanoe

```bash
brew install tippecanoe
```

### Docker or Podman

```bash
# Docker Desktop (most common)
brew install --cask docker
# Then launch Docker Desktop from Applications

# OR Podman (lighter weight, no daemon)
brew install podman
podman machine init
podman machine start
```

### Pull the GDAL image (one time)

```bash
docker pull ghcr.io/osgeo/gdal:alpine-small-latest
# or: podman pull ghcr.io/osgeo/gdal:alpine-small-latest
```

---

## Usage

```bash
# Basic — converts ZIP to .mbtiles in current directory
./s57-to-mbtiles.py NY_ENCs.zip

# Custom output name
./s57-to-mbtiles.py NY_ENCs.zip -o new-york-harbor.mbtiles

# Output directly to SignalK charts directory
./s57-to-mbtiles.py NY_ENCs.zip --output-dir ~/.signalk/charts-simple/

# Custom zoom range
./s57-to-mbtiles.py NY_ENCs.zip --minzoom 7 --maxzoom 14

# From a directory of .000 files instead of a ZIP
./s57-to-mbtiles.py ./my-enc-files/

# Keep temp GeoJSON files for debugging
./s57-to-mbtiles.py NY_ENCs.zip --keep-temp
```

## Where to get S-57 ENC charts

- **NOAA (US waters)**: https://charts.noaa.gov/ENCs/ENCs.shtml
- **Other nations**: Check your national hydrographic office
