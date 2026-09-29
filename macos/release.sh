#!/usr/bin/env bash
# Cut a release of the macOS app — one command, safe to re-run until it succeeds:
#
#   macos/release.sh            version from pyproject.toml, notes from docs/releases/v<version>.md
#
# 1. refuses unless the tree is clean, HEAD is pushed to origin/main, CI is green for HEAD and the
#    tag does not exist yet (PP_SKIP_CI=1 skips the CI check)
# 2. builds, signs, notarizes and staples the DMG (macos/build.sh) and self-tests the app
# 3. EdDSA-signs the final DMG bytes for Sparkle, writes appcast.xml and signs the feed too
# 4. creates the GitHub release as a DRAFT, uploads DMG + .sha256 + appcast.xml, checks them, and
#    only then publishes it — `releases/latest` never points at a release without its feed
# 5. checks what installed apps will see: the latest feed names this version, the DMG matches
#
# Needs: the Developer ID identity + notarytool profile (see docs/RELEASING.md) and the Sparkle
# EdDSA private key: ~/.config/pull-and-push/sparkle-ed25519-private.key (0600; PP_SPARKLE_KEY_FILE
# for another path), else the login keychain item (account "pull-and-push").
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
REPO="Lexus2016/Pull-and-Push"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
TAG="v$VERSION"
IFS=. read -r V_MAJ V_MIN V_PAT <<<"$VERSION"
BUILD=$((V_MAJ * 10000 + V_MIN * 100 + ${V_PAT:-0}))
NOTES="docs/releases/$TAG.md"
DMG="dist/Pull-and-Push-$VERSION-arm64.dmg"
SIGN_UPDATE="macos/.cache/Sparkle-2.10.0/bin/sign_update"
step() { printf '\n▶ %s\n' "$*"; }
die() { printf '✖ %s\n' "$*" >&2; exit 1; }

step "pre-flight for $TAG (build $BUILD)"
[ -f "$NOTES" ] || die "no release notes: $NOTES"
[ -z "$(git status --porcelain)" ] || die "the working tree is not clean"
git fetch -q origin main --tags
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "HEAD is not origin/main — push first"
! git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || die "tag $TAG already exists"
! gh release view "$TAG" -R "$REPO" >/dev/null 2>&1 || die "release $TAG already exists"
if [ "${PP_SKIP_CI:-}" != 1 ]; then
  ci="$(gh run list -R "$REPO" --commit "$(git rev-parse HEAD)" --json status,conclusion \
        --jq 'map(select(.status=="completed")) | .[0].conclusion // "none"')"
  [ "$ci" = success ] || die "CI for HEAD is '$ci' — wait for it (or PP_SKIP_CI=1)"
fi

step "build, sign, notarize"
macos/build.sh

step "self-test the signed build (no release unless it passes)"
macos/selftest.sh macos/build/Pull-and-Push.app

step "Sparkle signature of the final DMG"
# the key file (no keychain prompt, works unattended); the keychain item as the fallback
KEYFILE="${PP_SPARKLE_KEY_FILE:-$HOME/.config/pull-and-push/sparkle-ed25519-private.key}"
KEYARGS=(--account pull-and-push)
[ -f "$KEYFILE" ] && KEYARGS=(--ed-key-file "$KEYFILE")
[ -x "$SIGN_UPDATE" ] || die "missing $SIGN_UPDATE (macos/build.sh downloads Sparkle)"
SIG="$("$SIGN_UPDATE" "${KEYARGS[@]}" -p "$DMG")"
"$SIGN_UPDATE" "${KEYARGS[@]}" --verify "$DMG" "$SIG" >/dev/null || die "the DMG signature does not verify"

step "appcast.xml"
gh api markdown -f text="$(cat "$NOTES")" -f mode=gfm -f context="$REPO" > dist/notes.html
python3 macos/appcast.py --version "$VERSION" --build "$BUILD" --repo "$REPO" --dmg "$DMG" \
  --ed-signature "$SIG" --notes-html dist/notes.html --out dist/appcast.xml
"$SIGN_UPDATE" "${KEYARGS[@]}" dist/appcast.xml >/dev/null          # embeds the feed signature
"$SIGN_UPDATE" "${KEYARGS[@]}" --verify dist/appcast.xml >/dev/null || die "the feed signature does not verify"

step "GitHub release (draft → check → publish)"
TITLE="$TAG — $(sed -n 's/^# *//p' "$NOTES" | head -1)"
gh release create "$TAG" -R "$REPO" --draft --target "$(git rev-parse HEAD)" --title "$TITLE" \
  --notes-file "$NOTES" "$DMG" "$DMG.sha256" dist/appcast.xml
assets="$(gh release view "$TAG" -R "$REPO" --json assets --jq '[.assets[] | "\(.name)=\(.size)"] | join(" ")')"
for f in "$DMG" "$DMG.sha256" dist/appcast.xml; do
  want="$(basename "$f")=$(stat -f%z "$f")"
  [[ " $assets " == *" $want "* ]] || die "asset missing or truncated on the draft: $want (have: $assets)"
done
gh release edit "$TAG" -R "$REPO" --draft=false --latest >/dev/null

step "what installed apps will see"
feed="$(curl -fsSL "https://github.com/$REPO/releases/latest/download/appcast.xml")"
grep -q "<sparkle:version>$BUILD</sparkle:version>" <<<"$feed" || die "the latest feed does not name build $BUILD yet"
got="$(curl -fsSL "https://github.com/$REPO/releases/download/$TAG/$(basename "$DMG")" | shasum -a 256 | cut -c1-64)"
[ "$got" = "$(cut -c1-64 "$DMG.sha256")" ] || die "the published DMG differs from the built one"
echo "✔ $TAG is live: https://github.com/$REPO/releases/tag/$TAG"
