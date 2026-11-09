# Storefronts (per-store front)

```
storefronts/
  _default/                 # shared base behavior
    templates/
      base.html
      store.html
      category.html
      product.html
    static/
      style.css
    pages/
  <store-slug>/             # created/copied from a store pack
    templates/              # optional partial or full overrides
    static/                 # optional style.css / assets
    pages/                  # optional extra pages (landing, about, …)
```

Resolution: prefer `storefronts/<slug>/…` when the file exists; otherwise use `_default`.

The engine home (`/`) is not store-specific; it lives in `app/templates/`.
