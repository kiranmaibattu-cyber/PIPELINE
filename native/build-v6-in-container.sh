#!/usr/bin/env bash
set -euo pipefail

native_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
output="${1:-$native_root/lib}"
mkdir -p "$output"

ov_pkg="${OPENVINO_PYTHON_PACKAGE:-/usr/local/lib/python3.12/dist-packages/openvino}"
ov_lib_dir="$ov_pkg/libs"
ov_lib="$(find "$ov_lib_dir" -maxdepth 1 -name 'libopenvino.so.*' | sort | tail -1)"
if [[ -z "${ov_lib:-}" ]]; then
  echo "OpenVINO runtime library not found under $ov_lib_dir" >&2
  exit 1
fi

read -r -a gst_cflags <<< "$(pkg-config --cflags gstreamer-1.0)"
read -r -a gst_libs <<< "$(pkg-config --libs gstreamer-1.0)"

common=(-std=c++17 -O2 -DNDEBUG -fPIC -shared -Wl,-z,defs)
ocv="$native_root/ocvlib"

g++ "${common[@]}" -D_GLIBCXX_USE_CXX11_ABI=1 \
  -I"$native_root/headers/opencv4" "$native_root/src/gpu_motion_gate_fixed_slots_pipeline.cpp" \
  -Wl,-rpath,/opt/finalcombo/ocvlib -Wl,-rpath-link,"$ocv" \
  "$ocv/libopencv_video.so.406" "$ocv/libopencv_imgproc.so.406" "$ocv/libopencv_core.so.406" \
  /lib/x86_64-linux-gnu/libOpenCL.so.1 -o "$output/libgpu_motion_gate_fixed_slots.so"

for source in va_y_copy_bridge va_surface_raw va_surface_vehicle; do
  g++ "${common[@]}" -D_GLIBCXX_USE_CXX11_ABI=1 -I"$ov_pkg/include" \
    "${gst_cflags[@]}" "$native_root/src/$source.cpp" "${gst_libs[@]}" \
    "$ov_lib" /usr/lib/x86_64-linux-gnu/libva.so.2 \
    /lib/x86_64-linux-gnu/libOpenCL.so.1 -Wl,-rpath,/usr/local/lib/python3.12/dist-packages/openvino/libs \
    -o "$output/lib$source.so"
done

printf 'Rebuilt native V6 libraries in %s using %s\n' "$output" "$ov_lib"
