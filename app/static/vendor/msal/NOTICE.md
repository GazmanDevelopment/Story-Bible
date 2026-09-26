# MSAL.js for Browsers (vendored)

`msal-browser.min.js` is the built UMD distribution from the
[@azure/msal-browser](https://www.npmjs.com/package/@azure/msal-browser) npm
package (v3.30.0), used unmodified. Exposes a single global, `window.msal`.
Bundles `@azure/msal-common` (same license, same publisher).

Self-hosted here rather than loaded from a CDN, for the same reason as
`app/static/vendor/quill`: this app is deployed LAN/VPN-only (see PLAN.md),
and its own network policy shouldn't require reaching an external CDN just
to load the sign-in library.

License: MIT, see `LICENSE` in this folder - a permissive license that's
compatible with this project's GPLv3 (LICENSE at the repo root).

Used for #13 (nested app auth sign-in): `msal.createNestablePublicClientApplication`
inside Word when supported, `msal.PublicClientApplication` as the fallback
(a normal browser tab, or older Word via the dialog API) - see app/static/app.js.

To update: `npm pack @azure/msal-browser@<version>`, then copy
`lib/msal-browser.min.js` and the package's `LICENSE` over the files here.
