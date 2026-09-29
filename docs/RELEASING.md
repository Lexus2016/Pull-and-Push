# Releasing the macOS app

One command, from a clean, pushed `main` whose CI is green:

```bash
# 1. bump `version` in pyproject.toml and src/tyani_tolkai/__init__.py
# 2. write docs/releases/v<version>.md (the first `# heading` becomes the release title)
# 3. commit, push, wait for CI
macos/release.sh
```

`release.sh` builds and notarizes the DMG (`macos/build.sh`), signs it for Sparkle, writes
`appcast.xml` and signs the feed, creates the GitHub release as a draft, checks the uploaded
assets, publishes it, and finally reads the feed back the way installed apps do.

## How updates reach users

- The app's `SUFeedURL` is `https://github.com/Lexus2016/Pull-and-Push/releases/latest/download/appcast.xml`.
  Every release uploads its own `appcast.xml`, so "latest" always names the newest release; the
  DMG link inside it is pinned to that release's tag.
- `sparkle:version` / `CFBundleVersion` is an integer that only grows: `major*10000 + minor*100 + patch`
  (0.4.0 → 400). Never re-publish a version — bump the patch instead.
- Sparkle refuses an update unless the DMG matches its EdDSA signature, the feed is signed
  (`SURequireSignedFeed`), and the new app is signed with the same Developer ID.
- Drafts and pre-releases are not "latest": publishing a pre-release does not update anyone.

## Keys and credentials (never in the repo)

| what | where |
|---|---|
| Developer ID Application certificate | login keychain (`security find-identity -v -p codesigning`) |
| notarization credentials | notarytool keychain profile `pull-and-push` (`xcrun notarytool store-credentials pull-and-push …`); or `PP_NOTARY_APPLE_ID` / `PP_NOTARY_PASSWORD` / `PP_NOTARY_TEAM_ID` |
| Sparkle EdDSA private key | `~/.config/pull-and-push/sparkle-ed25519-private.key` (0600) + login keychain item, account `pull-and-push` |
| Sparkle EdDSA public key | `SPARKLE_PUBLIC_KEY` in `macos/build.sh` → `SUPublicEDKey` in Info.plist |

**Losing the EdDSA private key means installed apps can no longer verify updates** — keep a backup of
the key file (the macOS keychain item can be exported with
`macos/.cache/Sparkle-2.10.0/bin/generate_keys --account pull-and-push -x <file>`; import on another
Mac with `-f <file>`).

## Testing an update end to end

Build an older-looking copy with the same identity and let it update itself from the live feed:

```bash
PP_VERSION=0.3.99 macos/build.sh --no-notarize      # build 399, same bundle id and key
open macos/build/Pull-and-Push.app                   # ▸ Check for Updates… → Install and Relaunch
```
