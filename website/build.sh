#!/bin/sh
# Builds the GitHub Pages site: the landing page from website/ plus a live demo of the app at demo/.
#   sh website/build.sh [output directory]      (default: _site)
# The demo is the app's own index.html with demo.js loaded first - demo.js answers /api/* with fake data.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
out=${1:-_site}
mkdir -p "$out"
out=$(CDPATH= cd -- "$out" && pwd)
[ "$out" != "$root/website" ] && [ "$out" != "$root" ] || { echo "build.sh: pick another output directory" >&2; exit 1; }

# the landing page and its assets (everything in website/ except this script)
for f in "$root"/website/*; do
  [ "$(basename "$f")" = build.sh ] && continue
  cp -R "$f" "$out"/
done

# the demo: the app's page with <script src="demo.js"></script> right before its first <script
mkdir -p "$out/demo"
cp "$root/website/demo/demo.js" "$out/demo/demo.js"
# /favicon.svg is served by labmap.py; on Pages the site lives under /labmap/, so the page points at a copy next to it
if ! awk '
  !done && index($0, "<script") {
    i = index($0, "<script")
    print substr($0, 1, i - 1) "<script src=\"demo.js\"></script>"
    print substr($0, i)
    done = 1
    next
  }
  { sub(/href="\/favicon\.svg"/, "href=\"favicon.svg\""); print }
  END { if (!done) exit 1 }
' "$root/index.html" > "$out/demo/index.html"; then
  echo "build.sh: no <script> tag in index.html" >&2
  exit 1
fi
cp "$root/logo/labmap-icon.svg" "$out/demo/favicon.svg"
cp "$root/logo/labmap-icon.svg" "$out/favicon.svg"
: > "$out/.nojekyll"

echo "Built $out (demo at $out/demo/index.html)"
