#!/usr/bin/env bash
# Reference solution for `harbor run -a oracle`: a check of the image and the verifier, not of the game.
#
# It scaffolds /app with the Kotlin Toolchain's Compose Multiplatform template, the path the skill
# recommends, and adds a common test if the template has none. Expect a reward close to 1.0. If it is
# lower, read verifier/details.json and verifier/build.log in the trial folder: the cause is the image or
# the template, not an agent.
set -euo pipefail
cd "${LECHONK_APP_DIR:-/app}"

scaffolded=""
for cmd in "kotlin init compose-multiplatform" "kotlin new compose-multiplatform"; do
  echo "\$ $cmd"
  if $cmd < /dev/null && [ -n "$(find . -name module.yaml -not -path '*/build/*' | head -n 1)" ]; then
    scaffolded="$cmd"
    break
  fi
  find . -mindepth 1 -maxdepth 1 -exec rm -rf {} +
done
if [ -z "$scaffolded" ]; then
  echo "The toolchain's template commands failed: check 'kotlin init --help' and update this script." >&2
  exit 1
fi

# A template may create the project in a subfolder: move it up to /app.
project_yaml="$(find . -name project.yaml -not -path '*/build/*' | awk '{ print length($0) " " $0 }' | sort -n | head -n 1 | cut -d' ' -f2-)"
if [ -n "$project_yaml" ] && [ "$(dirname "$project_yaml")" != "." ]; then
  sub="$(dirname "$project_yaml")"
  (shopt -s dotglob && mv "$sub"/* .) && rmdir "$sub" 2>/dev/null || true
fi

# A common test in the shared module, if the template has none.
shared="$(grep -lE 'kmp/lib|(type|product): *lib *$' $(find . -name module.yaml -not -path '*/build/*') 2>/dev/null | head -n 1 || true)"
if [ -n "$shared" ]; then
  shared_dir="$(dirname "$shared")"
  if ! grep -rqs "@Test" "$shared_dir/test"; then
    mkdir -p "$shared_dir/test"
    cat > "$shared_dir/test/SmokeTest.kt" <<'EOF'
import kotlin.test.Test
import kotlin.test.assertEquals

class SmokeTest {
    @Test
    fun arithmeticStillWorks() {
        assertEquals(4, 2 + 2)
    }
}
EOF
  fi
fi

echo "oracle: scaffolded with '$scaffolded'; the verifier builds and tests it"
