# Vendored browser libraries

Served as they are from `/static/vendor/` (no build step, no CDN at runtime).

| Folder | What | Source | License |
| --- | --- | --- | --- |
| `react-18.3.1/` | `react.production.min.js`, `react-dom.production.min.js` (the UMD builds: `window.React`, `window.ReactDOM`) | The `react@18.3.1` and `react-dom@18.3.1` packages of the npm registry (`npm pack`, integrity checked), their `umd/` folders. 18.3 is the last React line with UMD builds | MIT (`react-18.3.1/LICENSE`) |

SHA-256 (checked by `tests/test_cart.py`):

```
d949f1c3687aedadcedac85261865f29b17cd273997e7f6b2bfc53b2f9d4c4dd  react.production.min.js
35f4f974f4b2bcd44da73963347f8952e341f83909e4498227d4e26b98f66f0d  react-dom.production.min.js
```

To update: `npm pack react@<version> react-dom@<version>`, copy the two files and the license into a new
`react-<version>/` folder, update the `<script>` tags in `app/storefronts/_default/templates/base.html` and the
hashes here and in `tests/test_cart.py`.
