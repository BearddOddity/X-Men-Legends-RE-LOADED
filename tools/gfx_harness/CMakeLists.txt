# Graphics runtime harness - drives src/d3d and src/nv2a with no game attached.
#
# Not part of the port. It links the host-side runtime only: no recompiled
# functions, no guest code, no static initialisers. See gfx_harness.c for why.
#
# Build standalone from the repo root:
#   cmake -S tools/gfx_harness -B build_gfx_harness -G Ninja
#   cmake --build build_gfx_harness
cmake_minimum_required(VERSION 3.20)
project(gfx_harness C)

set(CMAKE_C_STANDARD 11)

get_filename_component(REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)

add_subdirectory(${REPO_ROOT}/src/platform platform_build)
add_subdirectory(${REPO_ROOT}/src/d3d     d3d_build)
add_subdirectory(${REPO_ROOT}/src/nv2a    nv2a_build)

add_executable(gfx_harness gfx_harness.c)

target_include_directories(gfx_harness PRIVATE
    ${REPO_ROOT}/src/d3d
    ${REPO_ROOT}/src/nv2a
    ${REPO_ROOT}/src/platform
)

target_link_libraries(gfx_harness PRIVATE xbox_d3d8 xbox_nv2a platform)

if(MSVC)
    target_compile_definitions(gfx_harness PRIVATE _CRT_SECURE_NO_WARNINGS)
endif()
