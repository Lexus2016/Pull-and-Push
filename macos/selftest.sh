#!/usr/bin/env bash
# Headless check of a built app (no screen needed — CI runs it): a throwaway data dir with one
# project, the app in self-test mode (PP_SELFTEST, see main.swift), then the results and the
# shutdown are verified: the engine must be gone after Quit and after the app is SIGKILLed.
#   macos/selftest.sh [path/to/Pull-and-Push.app]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${1:-$ROOT/macos/build/Pull-and-Push.app}"
BIN="$APP/Contents/MacOS/Pull-and-Push"
CLI="$APP/Contents/Resources/bin/pull-and-push"
TMP="$(mktemp -d)"
trap 'pkill -f "$BIN" 2>/dev/null || true; rm -rf "$TMP"' EXIT
export TYANI_TOLKAI_HOME="$TMP/home"
# the engine's PID, from the dashboard marker it writes while it runs
engine_pid() { sed -n 's/.*"pid": *\([0-9]*\).*/\1/p' "$TYANI_TOLKAI_HOME/dashboard.json" 2>/dev/null || true; }
alive() { [ -n "$1" ] && kill -0 "$1" 2>/dev/null; }

"$CLI" research new "$TMP/kit" --name selftest >/dev/null
"$CLI" research create "$TMP/kit" >/dev/null

# launchd's bare environment, as when started from Finder: the app must find the agent CLIs
# through the login shell's PATH on its own
env -i HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" TMPDIR="$TMPDIR" PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  TYANI_TOLKAI_HOME="$TYANI_TOLKAI_HOME" PP_SELFTEST="$TMP/result.json" "$BIN" >"$TMP/app.log" 2>&1 &
app=$!
eng=""
for _ in $(seq 1 120); do
  [ -n "$eng" ] || eng="$(engine_pid)"
  kill -0 "$app" 2>/dev/null || break
  sleep 0.5
done
if kill -0 "$app" 2>/dev/null; then kill -9 "$app"; echo "✖ self-test timed out"; exit 1; fi
[ -f "$TMP/result.json" ] || { cat "$TMP/app.log"; echo "✖ no result"; exit 1; }
cat "$TMP/result.json"; echo
[ -n "$eng" ] || { echo "✖ never saw the engine's PID"; exit 1; }
sleep 1
! alive "$eng" || { echo "✖ the engine (pid $eng) outlived Quit"; exit 1; }

"$APP/Contents/Resources/python/bin/python3" - "$TMP/result.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
js = r.get("js") if isinstance(r.get("js"), dict) else {}
checks = {
    "dashboard ran in the WebView": bool(js),
    "no JS errors": js.get("errors") == [],
    "native bridge present": js.get("bridge") is True,
    "bridge refuses a missing path": js.get("reveal_missing") is False,
    "bridge reveals the data dir": js.get("reveal_home") is True,
    "native picker answers": js.get("pick") == "/selftest",
    "confirm() is wired": js.get("confirm") is True,
    "libraries are local": js.get("libs") == {"Chart": "function", "marked": "object",
                                              "DOMPurify": "function", "hljs": "object"},
    "fonts are local and loaded": js.get("font") is True and js.get("external") == [],
    "engines detected": isinstance(js.get("installed"), dict),
    # PP_EXPECT_AGENT=claude: that CLI is installed here, so a Finder-style launch must find it
    **({f"finds {a} from a bare environment": (js.get("installed") or {}).get(a) is True}
       if (a := __import__("os").environ.get("PP_EXPECT_AGENT")) else {}),
    "export downloaded": isinstance(r.get("download"), dict) and r["download"].get("bytes", 0) > 0,
    "settings panel renders": js.get("settings_sections") == 5,
    "no fatal error": "fatal" not in r and "engine" not in r,
}
for k, ok in checks.items():
    print(("✔ " if ok else "✖ ") + k)
sys.exit(0 if all(checks.values()) else 1)
PY

# updates: Sparkle is inside, signed, and pointed at the signed GitHub feed
plist="$APP/Contents/Info.plist"
[ -d "$APP/Contents/Frameworks/Sparkle.framework" ] || { echo "✖ Sparkle.framework missing"; exit 1; }
codesign --verify --strict "$APP/Contents/Frameworks/Sparkle.framework" || { echo "✖ Sparkle is not signed"; exit 1; }
feed="$(/usr/libexec/PlistBuddy -c 'Print :SUFeedURL' "$plist")"
key="$(/usr/libexec/PlistBuddy -c 'Print :SUPublicEDKey' "$plist")"
signed="$(/usr/libexec/PlistBuddy -c 'Print :SURequireSignedFeed' "$plist")"
build="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$plist")"
case "$feed" in https://github.com/*/releases/latest/download/appcast.xml) ;; *) echo "✖ feed: $feed"; exit 1;; esac
[ ${#key} -eq 44 ] && [ "$signed" = true ] && [[ "$build" =~ ^[0-9]+$ ]] \
  || { echo "✖ Sparkle keys: key=${#key} signed=$signed build=$build"; exit 1; }
echo "✔ updates: Sparkle signed, signed feed $feed, build $build"

# a crash / force-quit must not leave the engine (and its agents) running either
"$BIN" >/dev/null 2>&1 &
app=$!
eng=""
for _ in $(seq 1 60); do eng="$(engine_pid)"; alive "$eng" && break; sleep 0.5; done
alive "$eng" || { echo "✖ the engine did not start"; exit 1; }
kill -9 "$app"
for _ in $(seq 1 20); do alive "$eng" || break; sleep 0.5; done
! alive "$eng" || { echo "✖ the engine (pid $eng) outlived a SIGKILLed app"; exit 1; }
echo "✔ engine exits with the app (Quit and SIGKILL)"
