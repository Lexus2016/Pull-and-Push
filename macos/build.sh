#!/usr/bin/env bash
# Build Pull-and-Push.app for Apple Silicon and pack it into a signed, notarized DMG.
#
#   macos/build.sh                 Developer ID signature + notarization + stapled DMG (a release)
#   macos/build.sh --no-notarize   Developer ID signature, no notarization (local testing)
#   macos/build.sh --ad-hoc        ad-hoc signature: runs on this Mac only, no certificate needed
#
# The app = a Swift launcher (macos/Sources) + a relocatable CPython (python-build-standalone,
# pinned below) with this package installed into it. A real interpreter, not a frozen binary:
# scorers run as `{python} evaluate.py`.
#
# Env: PP_SIGN_IDENTITY   codesign identity (default: the first "Developer ID Application")
#      PP_NOTARY_PROFILE  keychain profile made once with `xcrun notarytool store-credentials`
#                         (default: pull-and-push)
#      PP_NOTARY_APPLE_ID + PP_NOTARY_PASSWORD (app-specific) + PP_NOTARY_TEAM_ID
#                         instead of a profile — for CI or a session that cannot write the keychain
# Output: dist/Pull-and-Push-<version>-arm64.dmg (+ .sha256)
set -euo pipefail

PBS_TAG=20260924
PBS_FILE="cpython-3.12.14+${PBS_TAG}-aarch64-apple-darwin-install_only.tar.gz"
PBS_SHA256=9763f43db2481a6af36af82ec40302aab7a73632f880129d07a6e81aec846277
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_FILE/+/%2B}"
NAME="Pull-and-Push"
BUNDLE_ID="com.lexus2016.pull-and-push"

MODE=notarize
for a in "$@"; do
  case "$a" in
    --no-notarize) MODE=sign ;;
    --ad-hoc) MODE=adhoc ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/macos/build"
CACHE="$ROOT/macos/.cache"
APP="$OUT/$NAME.app"
RES="$APP/Contents/Resources"
PY="$RES/python/bin/python3"
PYLIB="$RES/python/lib/python3.12"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
step() { printf '\n▶ %s\n' "$*"; }
die() { printf '✖ %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)/$(uname -m)" = "Darwin/arm64" ] || die "build on an Apple Silicon Mac"
command -v swiftc >/dev/null || die "swiftc not found — install Xcode or the Command Line Tools"
[ -n "$VERSION" ] || die "no version in pyproject.toml"

if [ "$MODE" = adhoc ]; then
  ID="-"
else
  ID="${PP_SIGN_IDENTITY:-$(security find-identity -v -p codesigning \
        | sed -n 's/.*"\(Developer ID Application: .*\)"/\1/p' | head -1)}"
  [ -n "$ID" ] || die "no 'Developer ID Application' identity in the keychain (or use --ad-hoc)"
fi
if [ "$MODE" = notarize ]; then
  PROFILE="${PP_NOTARY_PROFILE:-pull-and-push}"
  if [ -n "${PP_NOTARY_APPLE_ID:-}" ]; then
    [ -n "${PP_NOTARY_PASSWORD:-}" ] && [ -n "${PP_NOTARY_TEAM_ID:-}" ] \
      || die "PP_NOTARY_APPLE_ID needs PP_NOTARY_PASSWORD and PP_NOTARY_TEAM_ID"
    NOTARY=(--apple-id "$PP_NOTARY_APPLE_ID" --password "$PP_NOTARY_PASSWORD" --team-id "$PP_NOTARY_TEAM_ID")
  else
    NOTARY=(--keychain-profile "$PROFILE")
  fi
  xcrun notarytool history "${NOTARY[@]}" >/dev/null 2>&1 || die \
    "no notarytool profile '$PROFILE'. Create it once:
   xcrun notarytool store-credentials $PROFILE --apple-id <apple id> --team-id <team id> --password <app-specific password>
   (or build with --no-notarize)"
fi
echo "Pull-and-Push $VERSION — mode: $MODE — identity: $ID"

step "python-build-standalone ($PBS_FILE)"
mkdir -p "$CACHE"
TGZ="$CACHE/$PBS_FILE"
if [ ! -f "$TGZ" ]; then
  curl -fL --retry 3 -o "$TGZ.part" "$PBS_URL"
  mv "$TGZ.part" "$TGZ"
fi
echo "$PBS_SHA256  $TGZ" | shasum -a 256 -c - >/dev/null || { rm -f "$TGZ"; die "checksum mismatch: $TGZ"; }

step "bundle skeleton"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES/bin"
tar -xzf "$TGZ" -C "$RES"                                  # → Resources/python

step "wheels (downloaded by the host Python)"
# The bundled interpreter is a brand-new binary: an outbound firewall (Little Snitch & co.) holds
# its connections back, so everything is fetched by a known host Python and installed offline.
HOSTPY="${PP_HOST_PYTHON:-}"
if [ -z "$HOSTPY" ]; then
  for c in "$ROOT/.venv/bin/python" python3; do
    if command -v "$c" >/dev/null 2>&1; then HOSTPY="$(command -v "$c")"; break; fi
  done
fi
[ -n "$HOSTPY" ] || die "no host python3 to download wheels with (set PP_HOST_PYTHON)"
WHEELS="$OUT/wheels"
rm -rf "$WHEELS" && mkdir -p "$WHEELS"
export PIP_DISABLE_PIP_VERSION_CHECK=1
"$HOSTPY" -m pip wheel --quiet --no-deps -w "$WHEELS" "$ROOT"
rm -rf "$ROOT/build"                                        # setuptools' scratch dir in the source tree
WHL="$(ls "$WHEELS"/*.whl | head -1)"
"$HOSTPY" -m pip download --quiet -d "$WHEELS" --only-binary=:all: --platform macosx_13_0_arm64 \
  --python-version 3.12 --implementation cp --abi cp312 "$WHL[web]"

step "install pull-and-push $VERSION into the bundled Python (offline)"
"$PY" -m pip install --quiet --no-index --find-links "$WHEELS" --no-compile --no-warn-script-location \
  "$WHL[web]"
"$PY" - <<'CHECK'
from tyani_tolkai.web.server import STATIC
for f in ("index.html", "vendor/chart.umd.min.js", "vendor/fonts/fonts.css"):
    assert (STATIC / f).is_file(), f"{f} is missing from the installed package"
CHECK

step "prune what the app never uses"
rm -rf "$PYLIB"/{test,idlelib,tkinter,turtledemo,ensurepip} "$PYLIB"/lib-dynload/_tkinter*.so \
       "$RES/python"/lib/{tcl,tk,itcl,thread}* "$RES/python"/lib/lib{tcl,tk}* \
       "$RES/python"/{include,share} \
       "$PYLIB"/site-packages/{pip,pip-*.dist-info,setuptools,setuptools-*.dist-info,_distutils_hack} \
       "$PYLIB"/site-packages/distutils-precedence.pth
# console scripts carry this build machine's absolute interpreter path; the app uses `-m` instead
find "$RES/python/bin" -type f ! -name 'python3*' -delete
find "$RES/python/bin" -name 'python3*-config' -delete
find "$RES/python" -type l ! -exec test -e {} \; -delete   # links to what was pruned break codesign

step "byte-compile (unchecked-hash: never rewritten at run time, so the signature stays valid)"
find "$RES/python" -name __pycache__ -type d -prune -exec rm -rf {} +
"$PY" -m compileall -q -j0 --invalidation-mode unchecked-hash "$PYLIB" >/dev/null \
  || echo "  (a few files did not compile — ignored)"

step "command-line entry point (File ▸ Install the pull-and-push Command links it into ~/.local/bin)"
cat > "$RES/bin/pull-and-push" <<'SH'
#!/bin/sh
# pull-and-push from the macOS app: the bundled Python, never writing into the signed bundle
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 exec "$here/../python/bin/python3" -m tyani_tolkai.cli "$@"
SH
chmod 755 "$RES/bin/pull-and-push"

step "launcher (Swift)"
swiftc -O -swift-version 5 -target arm64-apple-macos13.0 -framework AppKit -framework WebKit \
  "$ROOT/macos/Sources/main.swift" -o "$APP/Contents/MacOS/$NAME"

step "icon + Info.plist"
ICONSET="$OUT/AppIcon.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
swift "$ROOT/macos/make_icon.swift" "$OUT/icon-1024.png"
for s in 16 32 128 256 512; do
  sips -z "$s" "$s" "$OUT/icon-1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$OUT/icon-1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$RES/AppIcon.icns"
sed -e "s/@VERSION@/$VERSION/g" -e "s/@BUNDLE_ID@/$BUNDLE_ID/g" "$ROOT/macos/Info.plist" \
  > "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist" >/dev/null
printf 'APPL????' > "$APP/Contents/PkgInfo"

step "smoke test the bundled engine"
PYTHONDONTWRITEBYTECODE=1 "$RES/bin/pull-and-push" --help >/dev/null
PYTHONDONTWRITEBYTECODE=1 "$PY" -c "import tyani_tolkai.web.server, sqlite3, ssl, ctypes; print('  engine imports OK')"

step "sign"
# hardened runtime (required for notarization) enforces library validation: every Mach-O must carry
# the same Team ID — an ad-hoc signature has none, so ad-hoc builds run without it
SIGN=(codesign --force --sign "$ID")
[ "$ID" = "-" ] || SIGN+=(--options runtime --timestamp)
# every Mach-O: the interpreter, libpython, stdlib and pip-installed extensions (pydantic-core …)
machos=()
while IFS= read -r -d '' f; do
  file -b "$f" | grep -q "Mach-O" && machos+=("$f")
done < <(find "$RES/python" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0)
echo "  ${#machos[@]} Mach-O files in the bundled Python"
for f in "${machos[@]}"; do "${SIGN[@]}" "$f" 2>/dev/null || "${SIGN[@]}" "$f"; done
"${SIGN[@]}" "$APP/Contents/MacOS/$NAME"
"${SIGN[@]}" "$APP"
codesign --verify --strict --deep "$APP"
for f in "${machos[@]}"; do codesign --verify --strict "$f" || die "bad signature: $f"; done
echo "  signature OK"

step "DMG"
STAGE="$OUT/dmg"
rm -rf "$STAGE" && mkdir -p "$STAGE" "$ROOT/dist"
ditto "$APP" "$STAGE/$NAME.app"
ln -s /Applications "$STAGE/Applications"
DMG="$ROOT/dist/$NAME-$VERSION-arm64.dmg"
rm -f "$DMG"
hdiutil create -volname "$NAME $VERSION" -srcfolder "$STAGE" -ov -format UDZO -fs HFS+ "$DMG" >/dev/null
[ "$ID" = "-" ] || codesign --force --sign "$ID" --timestamp "$DMG"

if [ "$MODE" = notarize ]; then
  step "notarize (usually 1–5 minutes)"
  xcrun notarytool submit "$DMG" "${NOTARY[@]}" --wait --output-format json \
    > "$OUT/notary.json" || true
  status="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status",""))' "$OUT/notary.json" 2>/dev/null || true)"
  if [ "$status" != "Accepted" ]; then
    cat "$OUT/notary.json" >&2
    sub="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id",""))' "$OUT/notary.json" 2>/dev/null || true)"
    [ -n "$sub" ] && xcrun notarytool log "$sub" "${NOTARY[@]}" >&2
    die "notarization: ${status:-failed}"
  fi
  xcrun stapler staple "$DMG" >/dev/null
  spctl -a -t open --context context:primary-signature -v "$DMG"
fi

(cd "$ROOT/dist" && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256")
step "done: $DMG ($(du -h "$DMG" | cut -f1))"
