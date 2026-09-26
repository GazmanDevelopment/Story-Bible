# Quill (vendored)

`quill.js` / `quill.snow.css` are the built distribution from the
[Quill](https://quilljs.com) npm package (v2.0.3), used unmodified. Self-hosted
here rather than loaded from a CDN, since this app is deployed LAN/VPN-only
(see PLAN.md) and its own network policy shouldn't require reaching an
external CDN just to load a form editor.

License: BSD 3-Clause, see `LICENSE` in this folder - a permissive license
that's compatible with this project's GPLv3 (LICENSE at the repo root).

`quill.js` also bundles a few of Quill's own runtime dependencies, all under
similarly permissive terms: `parchment` (BSD-3-Clause), `quill-delta`,
`eventemitter3` and `lodash-es` (all MIT).

To update: `npm pack quill@<version>`, then copy `dist/quill.js`,
`dist/quill.snow.css` and the package's `LICENSE` over the files here.
