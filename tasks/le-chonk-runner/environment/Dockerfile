# syntax=docker/dockerfile:1.7
#
# The agent's machine for Le Chonk Runner: Ubuntu 24.04 with the Kotlin Toolchain and the Android SDK.
#
# linux/amd64 on purpose: Android's build tools for Linux (aapt2 and others) are published for x86-64,
# so on an Apple Silicon Mac this image runs under emulation (Rosetta in Docker Desktop or OrbStack).
# If the oracle run also builds an APK on native arm64 for you, remove "--platform=linux/amd64" for speed.
FROM --platform=linux/amd64 ubuntu:24.04

# Empty means the latest release. Pin one with e.g. KOTLIN_TOOLCHAIN_VERSION=0.13.0.
ARG KOTLIN_TOOLCHAIN_VERSION=""
ARG ANDROID_CMDLINE_TOOLS_BUILD=11076708
# 0 skips the warm-up build (see warmup.sh): trials then download everything themselves.
ARG WARMUP=1

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    SDKMAN_DIR=/opt/sdkman \
    ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    KOTLIN_CLI_NO_WELCOME_BANNER=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git procps unzip xz-utils zip \
      python3 python3-yaml openjdk-17-jre-headless \
 && rm -rf /var/lib/apt/lists/* \
 && git config --global user.name "Agent" \
 && git config --global user.email "agent@example.com" \
 && git config --global init.defaultBranch main

# Android SDK command-line tools, with the SDK licences accepted: sdkmanager writes them to $ANDROID_HOME/licenses.
# The Kotlin Toolchain never accepts licences by itself, and a container has no one to answer a prompt.
RUN mkdir -p "$ANDROID_HOME/cmdline-tools" \
 && curl -fsSL -o /tmp/cmdline-tools.zip \
      "https://dl.google.com/android/repository/commandlinetools-linux-${ANDROID_CMDLINE_TOOLS_BUILD}_latest.zip" \
 && unzip -q /tmp/cmdline-tools.zip -d /tmp/cmdline-tools \
 && mv /tmp/cmdline-tools/cmdline-tools "$ANDROID_HOME/cmdline-tools/latest" \
 && rm -rf /tmp/cmdline-tools /tmp/cmdline-tools.zip \
 && (yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --licenses > /dev/null) \
 && "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" "platform-tools" > /dev/null

# The Kotlin Toolchain CLI, installed with SDKMAN, behind a small `kotlin` launcher on the PATH.
RUN curl -fsSL "https://get.sdkman.io?rcupdate=false" | bash \
 && printf 'sdkman_auto_answer=true\nsdkman_selfupdate_feature=false\nsdkman_colour_enable=false\n' >> "$SDKMAN_DIR/etc/config" \
 && bash -c 'source "$SDKMAN_DIR/bin/sdkman-init.sh" && sdk install kotlintoolchain $KOTLIN_TOOLCHAIN_VERSION' \
 && launcher="$(find -L "$SDKMAN_DIR/candidates/kotlintoolchain/current" -maxdepth 2 -type f -name kotlin | head -n 1)" \
 && test -n "$launcher" \
 && printf '#!/bin/sh\nexec "%s" "$@"\n' "$launcher" > /usr/local/bin/kotlin \
 && chmod +x /usr/local/bin/kotlin \
 && kotlin --version > /etc/kotlin-toolchain-version \
 && cat /etc/kotlin-toolchain-version

# Warm-up: scaffold and build a throwaway project once, so trials start with a full cache and accepted
# licences. The script is mounted, not copied, and the project is deleted, so agents find neither.
RUN --mount=type=bind,source=warmup.sh,target=/tmp/warmup.sh \
    if [ "$WARMUP" = "1" ]; then bash /tmp/warmup.sh; else echo "skipped" > /etc/le-chonk-warmup; fi

WORKDIR /app
