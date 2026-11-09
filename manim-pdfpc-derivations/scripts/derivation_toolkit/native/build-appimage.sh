#!/usr/bin/env bash
set -euo pipefail

packaging_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# Internal implementation: native/package.py validates and supplies these values.
fail() { printf '%s\n' "$*" >&2; exit 1; }
[[ $# == 10 ]] || fail 'Use the toolkit package command.'
source_root=$1 assets_dir=$2 work_dir=$3 output_file=$4 identity=$5
native_repository=$6 native_revision=$7 source_revision=$8 selection=$9 inputs=${10}
[[ $work_dir != / && $work_dir != "$HOME" ]] || fail 'Choose a dedicated packaging work directory.'

for command in python3 cmake ninja valac pkg-config curl sha256sum file find \
               desktop-file-validate patchelf readelf ldd sort flock; do
    command -v "$command" >/dev/null || fail "Missing build command: $command"
done
# Focal supplies glibc 2.31; compiling here sets the oldest supported runtime.
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 20.04 && $(uname -m) == x86_64 ]] || \
    fail 'Build this package in an Ubuntu 20.04 x86_64 container.'
pkg-config --exists gtk+-3.0 gee-0.8 poppler-glib json-glib-1.0 \
    pangocairo librsvg-2.0 gobject-introspection-1.0 || \
    fail 'Native build dependencies are missing; use the bundled Focal builder container.'

asset_args=(--project "$source_root" --assets "$assets_dir" --deck "$selection")
[[ -z $inputs ]] || asset_args+=(--inputs "$inputs")
python3 -B -m derivation_toolkit.package_assets "${asset_args[@]}"

mkdir -p -- "$work_dir" "$(dirname -- "$output_file")"
exec 9>"$work_dir/build.lock"
flock 9
tool_dir="$work_dir/tools"
appdir="$work_dir/AppDir"
if [[ -e $appdir && ! -f "$work_dir/.derivation-packaging" ]]; then
    fail "Existing AppDir is not owned by this builder: $appdir"
fi
touch "$work_dir/.derivation-packaging"
mkdir -p -- "$tool_dir"

fetch_tool() {
    local name=$1 checksum=$2 url=$3 destination="$tool_dir/$1"
    if [[ -f $destination ]] && \
       printf '%s  %s\n' "$checksum" "$destination" | sha256sum --check --status; then
        printf 'Using verified packaging tool: %s\n' "$name"
        return
    fi
    printf 'Fetching packaging tool %s from %s\n' "$name" "$url"
    curl --fail --location --silent --show-error --retry 3 \
        --proto '=https' --tlsv1.2 "$url" --output "$destination.download"
    printf '%s  %s\n' "$checksum" "$destination.download" | sha256sum --check --status || \
        fail "Checksum mismatch for $name"
    mv -- "$destination.download" "$destination"
    chmod 0755 "$destination"
}
while read -r name checksum url; do
    [[ -n $name && $name != \#* ]] || continue
    [[ $checksum =~ ^[0-9a-f]{64}$ && $url == https://* ]] || fail 'Invalid toolchain.lock entry'
    fetch_tool "$name" "$checksum" "$url"
done < "$packaging_dir/toolchain.lock"

# Extract the official build tools explicitly so CI does not need a FUSE mount.
for tool in linuxdeploy appimagetool; do
    tool_checksum=$(sha256sum -- "$tool_dir/$tool-x86_64.AppImage" | cut -d' ' -f1)
    extraction="$tool_dir/$tool-$tool_checksum-extracted"
    if [[ ! -x "$extraction/squashfs-root/AppRun" ]]; then
        temporary_extraction=$(mktemp -d "$tool_dir/$tool-extract.XXXXXX")
        if ! (cd -- "$temporary_extraction"; "$tool_dir/$tool-x86_64.AppImage" --appimage-extract >/dev/null); then
            rm -rf -- "$temporary_extraction"
            fail "Could not extract $tool"
        fi
        rm -rf -- "$extraction"
        mv -- "$temporary_extraction" "$extraction"
    fi
    case "$tool" in
        linuxdeploy) linuxdeploy="$extraction/squashfs-root/AppRun" ;;
        appimagetool) appimagetool="$extraction/squashfs-root/AppRun" ;;
    esac
done
export PATH="$tool_dir:$PATH"

native_info=$(python3 -B -m derivation_toolkit.native.build --project "$source_root" \
    --work-dir "$work_dir/native" --prepare-package)
native_source=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["source"])' <<< "$native_info")
native_build=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["build"])' <<< "$native_info")

# This directory only contains products created by this builder.
rm -rf -- "$appdir"
mkdir -p -- "$appdir"
DESTDIR="$appdir" cmake --install "$native_build"
payload="$appdir/usr/share/derivation-presentations"
mkdir -p -- "$payload" "$appdir/etc/fonts" "$appdir/usr/share/fonts/truetype" \
    "$appdir/usr/share/icons" "$appdir/usr/share/doc/$identity" \
    "$appdir/usr/lib/gio/modules"
python3 -B -m derivation_toolkit.package_assets "${asset_args[@]}" --destination "$payload"
mapfile -t asset_names < <(find "$payload" -type f -printf '%P\n' | LC_ALL=C sort)
install -m 0644 -- "$packaging_dir/fonts.conf" "$appdir/etc/fonts/fonts.conf.in"
[[ -d /usr/share/fonts/truetype/dejavu ]] || fail 'Install fonts-dejavu-core before packaging.'
cp -a -- /usr/share/fonts/truetype/dejavu "$appdir/usr/share/fonts/truetype/"
for icon_theme in Adwaita hicolor; do
    [[ -d /usr/share/icons/$icon_theme ]] || fail "Missing icon theme: $icon_theme"
    cp -a -- "/usr/share/icons/$icon_theme" "$appdir/usr/share/icons/"
done
[[ -f /usr/share/mime/mime.cache ]] || fail 'Install shared-mime-info before packaging.'
cp -a -- /usr/share/mime "$appdir/usr/share/"
for package in fonts-dejavu-core adwaita-icon-theme hicolor-icon-theme shared-mime-info; do
    install -D -m 0644 -- "/usr/share/doc/$package/copyright" \
        "$appdir/usr/share/doc/$package/copyright"
done
for license in COPYING LICENSE; do
    if [[ -f "$native_source/$license" ]]; then
        install -m 0644 -- "$native_source/$license" "$appdir/usr/share/doc/$identity/$license"
    fi
done
desktop_file="$work_dir/$identity.desktop"
package_icon="$work_dir/$identity.svg"
cp -- "$packaging_dir/presentation.svg" "$package_icon"
printf '[Desktop Entry]\nType=Application\nName=Animated presentations\nExec=%s\nIcon=%s\nCategories=Education;\nTerminal=false\n' \
    "$identity" "$identity" > "$desktop_file"
desktop-file-validate "$desktop_file"
DEPLOY_GTK_VERSION=3 "$linuxdeploy" --appdir "$appdir" \
    --executable "$appdir/usr/bin/pdfpc" \
    --desktop-file "$desktop_file" \
    --icon-file "$package_icon" --plugin gtk

# linuxdeploy assumes several desktop libraries are installed on the target.
# Include every remaining ELF dependency except the target's glibc components.
runtime_dependencies="$work_dir/runtime-dependencies.txt"
: > "$runtime_dependencies"
while IFS= read -r -d '' binary; do
    while read -r soname arrow resolved; do
        [[ $arrow == '=>' && $resolved == /* ]] || continue
        resolved=${resolved% \(0x*\)}
        case "$resolved" in "$appdir/"*) continue ;; esac
        case "$soname" in
            libc.so.*|libm.so.*|libpthread.so.*|libdl.so.*|librt.so.*|libresolv.so.*|\
            libutil.so.*|libanl.so.*|libBrokenLocale.so.*|libnss_*.so.*) continue ;;
        esac
        printf '%s\n' "$resolved" >> "$runtime_dependencies"
    done < <(LD_LIBRARY_PATH="$appdir/usr/lib" ldd "$binary" 2>/dev/null)
done < <(find "$appdir/usr" -type f \( -name '*.so*' -o -path '*/bin/*' \) -print0)
sort -u -o "$runtime_dependencies" "$runtime_dependencies"
extra_libraries=()
while IFS= read -r dependency; do
    extra_libraries+=(--library "$dependency")
done < "$runtime_dependencies"
if ((${#extra_libraries[@]})); then
    "$linuxdeploy" --appdir "$appdir" "${extra_libraries[@]}"
fi
rm -f -- "$appdir/AppRun"
install -m 0755 -- "$packaging_dir/AppRun" "$appdir/AppRun"

build_timestamp=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
printf '%s\n' "$source_revision" > "$payload/source-revision"
printf '%s\n' "$build_timestamp" > "$payload/build-timestamp"
bundle_id=$({
    printf 'native %s\n' "$native_revision"
    printf 'source %s\n' "$source_revision"
    sha256sum -- "$packaging_dir/AppRun" | cut -d' ' -f1
    sha256sum -- "$packaging_dir/fonts.conf" | cut -d' ' -f1
    for asset in "${asset_names[@]}"; do
        printf '%s ' "$asset"
        sha256sum -- "$payload/$asset" | cut -d' ' -f1
    done
} | sha256sum | cut -d' ' -f1)
printf '%s\n' "$bundle_id" > "$payload/bundle-id"

# Check the symbol baseline of every packaged ELF library and executable.
glibc_versions="$work_dir/glibc-versions.txt"
: > "$glibc_versions"
while IFS= read -r -d '' binary; do
    readelf --version-info "$binary" 2>/dev/null | \
        sed -n 's/.*Name: GLIBC_\([0-9][0-9.]*\).*/\1/p' >> "$glibc_versions" || true
done < <(find "$appdir/usr" -type f \( -name '*.so*' -o -path '*/bin/*' \) -print0)
highest_glibc=$(sort -Vu "$glibc_versions" | tail -n1)
[[ -n $highest_glibc && $(printf '2.31\n%s\n' "$highest_glibc" | sort -V | tail -n1) == 2.31 ]] || \
    fail "Packaged ELF files require GLIBC_$highest_glibc, newer than the Focal baseline."

build_info="$payload/build-info.txt"
{
    printf 'Source revision: %s\nBuild timestamp: %s\n' "$source_revision" "$build_timestamp"
    printf 'Native repository: %s\nNative revision: %s\n' "$native_repository" "$native_revision"
    printf 'Architecture: x86_64\nBuild OS: %s\nHighest required GLIBC: %s\n' "$PRETTY_NAME" "$highest_glibc"
    printf 'CMake options: Release; MDVIEW=OFF; REST=OFF; MOVIES=OFF; BUILD_TESTING=OFF\n'
    printf 'Bundle ID: %s\n\nOfficial packaging tools:\n' "$bundle_id"
    cat "$packaging_dir/toolchain.lock"
    printf '\nAdditional runtime libraries:\n'
    cat "$runtime_dependencies"
    printf '\nBuild-container packages:\n'
    dpkg-query -W -f='${Package} ${Version}\n'
} > "$build_info"
cp -- "$build_info" "$output_file.build-info.txt"
ARCH=x86_64 "$appimagetool" --runtime-file "$tool_dir/runtime-x86_64" \
    --comp zstd "$appdir" "$output_file"
chmod 0755 "$output_file"
(
    cd -- "$(dirname -- "$output_file")"
    sha256sum -- "$(basename -- "$output_file")"
) > "$output_file.sha256"
printf 'Built %s\nNative revision: %s\nBundle ID: %s\n' \
    "$output_file" "$native_revision" "$bundle_id"
