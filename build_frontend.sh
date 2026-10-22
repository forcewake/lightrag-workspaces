#!/usr/bin/env bash
# Rebuild the UI from the pinned upstream source plus this repository's patch.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pinned_revision=28ff1b05f2ac3f3e6fa14dd2cd33656579bd0c9c
upstream_url=https://github.com/HKUDS/LightRAG.git
bun_bin="${BUN_BIN:-bun}"
output_dir="${1:-${project_dir}/webui}"
while [[ "$output_dir" != '/' && "$output_dir" == */ ]]; do
  output_dir="${output_dir%/}"
done

if [[ $# -gt 1 ]]; then
  printf 'Usage: %s [output-directory]\n' "$0" >&2
  exit 2
fi
for required_tool in git tar "$bun_bin"; do
  command -v "$required_tool" >/dev/null || {
    printf 'Required tool is unavailable: %s\n' "$required_tool" >&2
    exit 1
  }
done
if [[ "$("$bun_bin" --version)" != "1.3.3" ]]; then
  printf 'This source pin requires Bun 1.3.3. Use the Docker build for a managed toolchain.\n' >&2
  exit 1
fi
if [[ ! -f "${project_dir}/frontend.patch" ]]; then
  printf 'Missing frontend.patch beside this script.\n' >&2
  exit 1
fi

build_root="$(mktemp -d "${TMPDIR:-/tmp}/lightrag-frontend.XXXXXX")"
publish_stage=''
cleanup() {
  rm -rf "$build_root"
  if [[ -n "$publish_stage" && -d "$publish_stage" ]]; then
    rm -rf "$publish_stage"
  fi
}
trap cleanup EXIT
mkdir "${build_root}/source"

# An existing clone permits an entirely local rebuild. Otherwise fetch only
# the immutable commit; neither a moving branch nor a release tag is trusted.
if [[ -n "${LIGHTRAG_SOURCE_REPOSITORY:-}" ]]; then
  source_repo="$LIGHTRAG_SOURCE_REPOSITORY"
  git -C "$source_repo" cat-file -e "${pinned_revision}^{commit}"
else
  source_repo="${build_root}/upstream"
  git init --quiet "$source_repo"
  git -C "$source_repo" fetch --quiet --depth=1 "$upstream_url" "$pinned_revision"
  actual_revision="$(git -C "$source_repo" rev-parse FETCH_HEAD)"
  [[ "$actual_revision" == "$pinned_revision" ]] || {
    printf 'Fetched revision does not match the pinned source.\n' >&2
    exit 1
  }
fi
git -C "$source_repo" archive "$pinned_revision" | tar -x -C "${build_root}/source"
git -C "${build_root}/source" apply --check "${project_dir}/frontend.patch"
git -C "${build_root}/source" apply "${project_dir}/frontend.patch"

printf 'Building LightRAG v1.5.7 at %s with Bun %s\n' "$pinned_revision" "$("$bun_bin" --version)"
(
  cd "${build_root}/source/lightrag_webui"
  "$bun_bin" install --frozen-lockfile
  "$bun_bin" test
  "$bun_bin" x tsc --noEmit
  "$bun_bin" run lint
  "$bun_bin" run build
)

# Publish only after every check succeeds. A temporary sibling keeps a failed
# copy from replacing the previous usable build; no deployment is performed.
output_name="$(basename "$output_dir")"
if [[ "$output_name" == '.' || "$output_name" == '..' || "$output_name" == '/' ]]; then
  printf 'Choose a dedicated output directory.\n' >&2
  exit 1
fi
mkdir -p "$(dirname "$output_dir")"
output_parent="$(cd "$(dirname "$output_dir")" && pwd)"
output_dir="${output_parent}/${output_name}"
if [[ -L "$output_dir" || ( -e "$output_dir" && ! -d "$output_dir" ) ]]; then
  printf 'Output must be a directory, not a file or symbolic link: %s\n' "$output_dir" >&2
  exit 1
fi
publish_stage="$(mktemp -d "${output_parent}/.lightrag-webui.XXXXXX")"
cp -R "${build_root}/source/lightrag/api/webui/." "${publish_stage}/"
previous_dir=''
if [[ -d "$output_dir" ]]; then
  previous_dir="$(mktemp -d "${output_parent}/.lightrag-webui-previous.XXXXXX")"
  rmdir "$previous_dir"
  mv "$output_dir" "$previous_dir"
fi
if ! mv "$publish_stage" "$output_dir"; then
  if [[ -n "$previous_dir" ]]; then
    mv "$previous_dir" "$output_dir"
  fi
  exit 1
fi
publish_stage=''
if [[ -n "$previous_dir" ]]; then
  rm -rf "$previous_dir"
fi
printf 'Verified frontend written to %s\n' "$output_dir"
