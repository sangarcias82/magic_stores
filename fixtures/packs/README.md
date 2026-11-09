# Store packs

A versioned pack materializes:

1. Catalog data (store, categories, property_definitions, products, …)
2. Optionally a **dedicated storefront**:

```
pack/
  store-pack.v1.json
  storefront/                 # copied to app/storefronts/<slug>/
    templates/                # overrides (e.g. store.html, product.html)
    static/style.css          # look and feel for that store
    pages/about.html          # extra pages
```

Without a `storefront/` folder, the store uses `_default` only.
