#!/usr/bin/env bash
set -euo pipefail
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  clang lld cmake ninja-build meson pkg-config git curl zip unzip \
  autoconf automake libtool bison flex libjson-perl wine64-tools \
  libvulkan-dev spirv-headers glslang-tools libssl-dev patchelf \
  libx11-dev libxext-dev libxrandr-dev libxcursor-dev libxi-dev \
  libxfixes-dev libxss-dev libxtst-dev libxkbcommon-dev libwayland-dev \
  libdecor-0-dev libasound2-dev libpulse-dev libudev-dev libdrm-dev libgbm-dev
if ! command -v widl >/dev/null; then
  sudo ln -s /usr/lib/wine/widl /usr/local/bin/widl
fi
