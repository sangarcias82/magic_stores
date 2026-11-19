// Cart logic without React or the DOM (tests/js/cart-core.test.js runs it with Node): the guards
// against rage clicks and the mapping of API errors to messages. Loaded before cart.js.
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.MagicCartCore = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // An Idempotency-Key for one user action (a click): the server answers copies of it once.
  function newKey() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) {
      return crypto.randomUUID();
    }
    return "k" + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }

  // One action at a time per name: while "add" runs, more clicks on it are ignored (not queued),
  // so a rage click sends one request. onChange() is called when an action starts or ends.
  function createActionGuard(onChange) {
    var running = new Set();
    function changed() {
      if (onChange) {
        onChange();
      }
    }
    return {
      isRunning: function (name) {
        return running.has(name);
      },
      run: function (name, action) {
        if (running.has(name)) {
          return Promise.resolve({ skipped: true });
        }
        running.add(name);
        changed();
        return Promise.resolve()
          .then(action)
          .finally(function () {
            running.delete(name);
            changed();
          });
      },
    };
  }

  // Quantity clicks on a line: each change restarts a short timer, and only the last number is
  // sent when the clicks stop (five quick "+" = one request asking for +5). Every push() gets the
  // promise of the request that finally carries it.
  function createBatcher(send, delay, timers) {
    var setT = (timers && timers.set) || setTimeout;
    var clearT = (timers && timers.clear) || clearTimeout;
    var pending = new Map(); // id -> {timer, value, waiters: [{resolve, reject}]}
    function fire(id) {
      var entry = pending.get(id);
      pending.delete(id);
      Promise.resolve()
        .then(function () {
          return send(id, entry.value);
        })
        .then(
          function (result) {
            entry.waiters.forEach(function (w) { w.resolve(result); });
          },
          function (error) {
            entry.waiters.forEach(function (w) { w.reject(error); });
          }
        );
    }
    return {
      push: function (id, value) {
        var entry = pending.get(id);
        if (entry) {
          clearT(entry.timer);
        } else {
          entry = { waiters: [] };
          pending.set(id, entry);
        }
        entry.value = value;
        entry.timer = setT(function () { fire(id); }, delay);
        return new Promise(function (resolve, reject) {
          entry.waiters.push({ resolve: resolve, reject: reject });
        });
      },
      isPending: function (id) {
        return pending.has(id);
      },
    };
  }

  var KNOWN_ERRORS = ["out_of_stock", "insufficient_stock", "unknown_product", "quantity_limit", "cart_full",
                      "request_in_progress",
                      // checkout (POST /cart/checkout)
                      "cart_empty", "unavailable", "price_changed", "unknown_method", "inactive_method",
                      "country_required", "country_not_served", "unknown_terminal", "terminal_disabled",
                      "nothing_to_pay", "total_too_large", "terminal_not_configured"];

  // The message key (in the page's texts) for a refused request.
  function errorKey(status, body) {
    var item = body && Array.isArray(body.detail) ? body.detail[0] : null;
    var type = item && item.type;
    if (KNOWN_ERRORS.indexOf(type) !== -1) {
      return "error." + type;
    }
    return status === 422 ? "error.invalid" : "error.generic";
  }

  // The "add by SKU or id" form: what to send for the chosen kind of reference.
  function productRef(kind, text) {
    var value = String(text || "").trim();
    if (!value) {
      return null;
    }
    if (kind === "id") {
      return /^[0-9]{1,18}$/.test(value) ? { product_id: parseInt(value, 10) } : null;
    }
    return { sku: value };
  }

  // A quantity typed or stepped by the visitor, kept within 1..max.
  function clampQuantity(value, max) {
    var n = parseInt(value, 10);
    if (isNaN(n) || n < 1) {
      return 1;
    }
    return Math.min(n, max);
  }

  // What the server shows for money ("MXN 6,499.00": app/services/catalog.format_price).
  function formatMoney(cents, currency) {
    var units = Math.floor(Math.abs(cents) / 100);
    var rest = String(Math.abs(cents) % 100);
    var grouped = String(units).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return currency + " " + (cents < 0 ? "-" : "") + grouped + "." + (rest.length < 2 ? "0" + rest : rest);
  }

  // The checkout body: the shipping method and the terminal, chosen separately; the country only
  // when given (2 letters, upper case). null if the method or the terminal is missing.
  function checkoutBody(method, country, terminal) {
    if (!method || !terminal) {
      return null;
    }
    var body = { shipping_method: method, terminal: terminal };
    var code = String(country || "").trim().toUpperCase();
    if (code) {
      body.country = code;
    }
    return body;
  }

  // The checkout's refusal for some lines: which lines (ids), to show them in the cart.
  function refusedLines(body) {
    var items = body && Array.isArray(body.detail) ? body.detail : [];
    return items.filter(function (item) {
      return Array.isArray(item.loc) && item.loc[0] === "cart" && item.loc[1] === "lines";
    }).map(function (item) { return item.loc[2]; });
  }

  // A text in the page's language: its translation for ``lang`` when there is one ({"en": "…"}),
  // else the text as entered (product names, the bank transfer note).
  function translated(translations, lang, text) {
    var found = translations && Object.prototype.hasOwnProperty.call(translations, lang) ? translations[lang] : "";
    return found || text;
  }

  // A shipping method's / terminal's name in the page's language: the page gives {kind: {code: name}}.
  function nameOf(names, kind, item) {
    return translated((names || {})[kind], item.code, item.name);
  }

  // "{count} items" with the page's text.
  function format(text, params) {
    return String(text).replace(/\{(\w+)\}/g, function (all, name) {
      return params && Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : all;
    });
  }

  return {
    newKey: newKey,
    createActionGuard: createActionGuard,
    createBatcher: createBatcher,
    errorKey: errorKey,
    productRef: productRef,
    clampQuantity: clampQuantity,
    format: format,
    formatMoney: formatMoney,
    checkoutBody: checkoutBody,
    refusedLines: refusedLines,
    translated: translated,
    nameOf: nameOf,
    MAX_QUANTITY: 99,
    QUANTITY_DELAY_MS: 400,
  };
});
