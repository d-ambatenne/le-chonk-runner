#!/usr/bin/env bash
# Runs once while the image is built (see the Dockerfile), never during a trial.
#
# It scaffolds and builds a throwaway Kotlin Toolchain project, so that trials start with the toolchain,
# the Compose and Android dependencies and the Android SDK packages in the cache, and with the SDK
# licences accepted, including those of an SDK the toolchain provisions in its own cache.
#
# The outcome goes to /etc/le-chonk-warmup, which the verifier reports with every trial. The script
# always exits 0: a failed warm-up makes trials slower or breaks their Android builds, and the oracle
# run (`harbor run -a oracle`) shows it, but it doesn't stop the image from building.
set -u

# The paths can be changed for testing the script outside the image.
LOG="${LECHONK_WARMUP_LOG:-/var/log/le-chonk-warmup.log}"
STATUS_FILE="${LECHONK_WARMUP_STATUS:-/etc/le-chonk-warmup}"
WORK="${LECHONK_WARMUP_DIR:-/tmp/le-chonk-warmup}"
SDK_SEARCH="${LECHONK_SDK_SEARCH:-/opt /root}"
: > "$LOG"

log() { echo "[warmup] $*" | tee -a "$LOG" >&2; }

java_home() { dirname "$(dirname "$(readlink -f "$(command -v java)")")"; }

accept_licences() {
  local sdkmanager
  while IFS= read -r sdkmanager; do
    log "accepting Android SDK licences with $sdkmanager"
    (yes | JAVA_HOME="$(java_home)" "$sdkmanager" --licenses >> "$LOG" 2>&1) || true
  # shellcheck disable=SC2086
  done < <(find $SDK_SEARCH -type f -path '*/bin/sdkmanager' 2>/dev/null | sort -u)
}

# Prints the name of the first module whose module.yaml matches the pattern.
module_with() {
  local file
  while IFS= read -r file; do
    if grep -Eq "$1" "$file"; then basename "$(dirname "$file")"; return 0; fi
  done < <(find "$WORK" -name module.yaml -not -path '*/build/*' | sort)
  return 1
}

has_modules() { [ -n "$(find "$WORK" -name module.yaml -not -path '*/build/*' | head -n 1)" ]; }

# The toolchain's own Compose Multiplatform template, without prompts. Prints how it scaffolded.
scaffold_template() {
  local cmd
  for cmd in "kotlin init compose-multiplatform" "kotlin new compose-multiplatform"; do
    rm -rf "$WORK" && mkdir -p "$WORK"
    log "\$ $cmd"
    if (cd "$WORK" && $cmd < /dev/null >> "$LOG" 2>&1) && has_modules; then
      echo "template: $cmd"
      return 0
    fi
  done
  return 1
}

# Fallback: a minimal Android app, enough to provision the Android SDK and accept its licences.
scaffold_android_only() {
  rm -rf "$WORK" && mkdir -p "$WORK/android-app/src"
  printf 'modules:\n  - ./android-app\n' > "$WORK/project.yaml"
  printf 'product: android/app\n\nsettings:\n  android:\n    namespace: org.example.warmup\n' > "$WORK/android-app/module.yaml"
  cat > "$WORK/android-app/src/AndroidManifest.xml" <<'EOF'
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <application android:label="Warmup">
        <activity android:name="org.example.warmup.MainActivity" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
EOF
  cat > "$WORK/android-app/src/MainActivity.kt" <<'EOF'
package org.example.warmup

import android.app.Activity

class MainActivity : Activity()
EOF
  echo "fallback: minimal android/app"
}

build_android() {
  log "\$ kotlin build -m $1"
  (cd "$ROOT" && kotlin build -m "$1" < /dev/null >> "$LOG" 2>&1)
}

accept_licences
how="$(scaffold_template)" || how="$(scaffold_android_only)"
log "scaffold: $how"
# A template may create the project in a subfolder: build from the folder that holds project.yaml.
ROOT="$(find "$WORK" -name project.yaml -not -path '*/build/*' | awk '{ print length($0) " " $0 }' | sort -n | head -n 1 | cut -d' ' -f2-)"
ROOT="$(dirname "${ROOT:-$WORK/project.yaml}")"

status="ok"
android="$(module_with 'android/app')" || android=""
if [ -z "$android" ]; then
  status="no android/app module after scaffolding"
elif ! build_android "$android"; then
  # A licence failure leaves the SDK the toolchain just provisioned on disk: accept its licences and retry.
  accept_licences
  build_android "$android" || status="android build failed"
fi

shared="$(module_with 'kmp/lib|(type|product): *lib *$')" || shared=""
if [ -n "$shared" ]; then
  log "\$ kotlin test -m $shared"
  (cd "$ROOT" && kotlin test -m "$shared" < /dev/null >> "$LOG" 2>&1) || log "shared tests failed in the warm-up (not fatal)"
fi
accept_licences

echo "$status ($how; $(kotlin --version 2>/dev/null | head -n 1); $(date -u +%Y-%m-%dT%H:%M:%SZ))" > "$STATUS_FILE"
log "warm-up: $(cat "$STATUS_FILE")"
if [ "$status" != "ok" ]; then
  log "last lines of $LOG:"
  tail -n 60 "$LOG" >&2
fi
rm -rf "$WORK"
exit 0
