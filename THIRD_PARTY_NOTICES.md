# Third-party notices

LightRAG Workspaces is an independent extension of
[HKUDS/LightRAG](https://github.com/HKUDS/LightRAG), not an official LightRAG
release. Names identify compatibility; they do not imply endorsement.

The extension's original code and documentation are licensed under the
[MIT License](LICENSE). Third-party code retains its own license and notices.

## LightRAG and its WebUI

- Upstream: <https://github.com/HKUDS/LightRAG>
- Pinned source: [`28ff1b05f2ac3f3e6fa14dd2cd33656579bd0c9c`](https://github.com/HKUDS/LightRAG/tree/28ff1b05f2ac3f3e6fa14dd2cd33656579bd0c9c), version 1.5.7.
- License: [MIT at that revision](https://github.com/HKUDS/LightRAG/blob/28ff1b05f2ac3f3e6fa14dd2cd33656579bd0c9c/LICENSE).
- Use: `frontend.patch` modifies the upstream WebUI and contains upstream
  context lines. The build fetches that exact source and applies the patch.
  The worker integrates with the upstream Python server in the pinned base
  image. Generated WebUI bundles and the upstream source tree are not committed
  to this repository.

The upstream notice is reproduced below and must accompany copies or
substantial portions of the upstream software, including modified copies:

```text
MIT License

Copyright (c) 2025 LightRAG Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## nano-vectordb

- Upstream: <https://github.com/gusye1234/nano-vectordb>
- Runtime version: `0.0.4.3`, inherited from the pinned LightRAG image.
- License declaration: `License :: OSI Approved :: MIT License` in the
  [official release metadata](https://pypi.org/pypi/nano-vectordb/0.0.4.3/json).
- Use: `worker.py` replaces its startup loader and constructor normalization
  hooks with this extension's bounded-memory implementation. It uses the native
  data format; the package itself is not vendored into this source repository.

The official 0.0.4.3 wheel and source archive declare MIT in their metadata but
contain no separate LICENSE file. The upstream repository's
[current MIT notice](https://github.com/gusye1234/nano-vectordb/blob/main/LICENSE)
is maintained by that project. This document does not assign that current
notice's year to the older release.

## Build and runtime dependencies

The Docker build extends the official LightRAG image. It retains the image's
installed dependencies; it does not relicense them. The frontend build installs
packages from the pinned upstream `lightrag_webui/package.json` and frozen
`bun.lock`. Their license terms remain applicable to generated assets.

This is a source distribution of the extension, not a complete license
inventory or SBOM for the inherited operating system, Python packages, or
frontend packages. If you distribute a built image or generated WebUI, retain
applicable upstream notices and include the notices required by the dependency
versions actually distributed. Review that inventory again when updating the
base image or frontend lockfile.

## Documentation site

The documentation uses **Material for MkDocs 9.6.23**, pinned in
`requirements-docs.txt`. The theme is MIT licensed by Martin Donath; its footer
attribution remains enabled. The site's font configuration disables external
font loading.

Unmodified license notices from the exact theme wheel are retained under
[`docs/assets/licenses/`](docs/assets/licenses/), including notices for the
Pictogrammers Material, Font Awesome, Octicons, and Simple Icons sets bundled
with the theme. Selected theme icons are embedded in generated pages. Their
licenses remain separate from this project's MIT license; see the site's
[credits](docs/credits.md) and the copied notice files. The full Apache 2.0
license is included alongside the Material icon notice.
