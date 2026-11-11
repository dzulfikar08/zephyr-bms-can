# SPDX-License-Identifier: MIT
cmake_minimum_required(VERSION 3.20.0)

# Pulls in Zephyr's build system (ZEPHYR_BASE from scripts/env.sh).
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(zephyr_bms LANGUAGES C)

target_sources(app PRIVATE src/main.c src/pack.c)
