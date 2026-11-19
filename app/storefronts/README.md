# Storefronts (per-store front)

```
storefronts/
  _default/                 # shared base behavior
    templates/
      base.html
      store.html
      category.html
      product.html
      cart.html                 # the cart page (filled by app/static/cart/cart.js)
      order.html                # an order as the shopper sees it (bank transfer instructions)
      _language_switcher.html   # language switcher links (UI text: see app/locales/)
    static/
      style.css
    pages/
  solares/                  # committed: the solares store's storefront (same as fixtures/packs/solares/storefront/)
  <store-slug>/             # copied from a store pack (storefront.copy_from_pack_dir), or added by hand
    templates/              # optional partial or full overrides
    static/                 # optional style.css / assets
    pages/                  # optional extra pages (landing, about, …)
```

Resolution: prefer `storefronts/<slug>/…` when the file exists; otherwise use `_default`.

A pack import with its folder (command line, or a `.zip` upload in admin) copies the pack's storefront folder here and **replaces** `<store-slug>/` as a whole, so don't
hand-edit a folder that comes from a pack (change the pack instead). Hidden `.<slug>.staging-*` / `.<slug>.old-*`
folders only exist for a moment during an import. Rules: `fixtures/packs/README.md`, "Storefront folder".

The engine home (`/`) is not store-specific; it lives in `app/templates/`.

Phones: each store stylesheet has a `@media (max-width: 640px)` block (one narrow column, 17 px text, small side
margins, a header that scrolls away, 44 px links); the cart's phone rules are shared in `app/static/cart/cart.css`,
which `base.html` loads on every storefront page. A store with its own `style.css` (like solares) brings its own
phone block. `solares/` must stay identical to `fixtures/packs/solares/storefront/`: change one, copy it to the other
(`tests/test_seed_packs.py` and `tests/test_storefront_mobile.py` compare them).
