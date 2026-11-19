// The public cart (React 18, no build step: React.createElement). Three parts, on storefront pages:
// - the header count ([data-cart-count]),
// - "Add to cart" on product pages (#cart-add),
// - the cart page (#cart-app): lines with picture, SKU, quantity stepper, remove, subtotal, and
//   "add by SKU or product id".
// Server API: /api/stores/<slug>/cart (app/routers/cart.py). Texts come from the page
// (#cart-config, the store's language). Rage clicks: see cart-core.js.
(function () {
  "use strict";
  var configElement = document.getElementById("cart-config");
  if (!configElement || !window.React || !window.ReactDOM || !window.MagicCartCore) {
    return;
  }
  var config = JSON.parse(configElement.textContent);
  var core = window.MagicCartCore;
  var h = React.createElement;
  var useState = React.useState;
  var useEffect = React.useEffect;
  var useRef = React.useRef;
  var CART_EVENT = "magic-stores:cart";

  function text(key, params) {
    return core.format(config.texts[key] || key, params);
  }

  // A shipping method's / terminal's name in the page language (the page gives them; the JSON API
  // answers with the names as entered).
  function nameOf(kind, item) {
    return core.nameOf(config.names, kind, item);
  }

  // --------------------------------------------------------------------------- the API

  // Plain JSON request to another store API (shipping methods / quote, payment terminals): no cart broadcast.
  function fetchJson(method, url, body, key) {
    var headers = { Accept: "application/json" };
    if (body !== undefined) { headers["Content-Type"] = "application/json"; }
    if (key) { headers["Idempotency-Key"] = key; }
    return fetch(url, {
      method: method, credentials: "same-origin", headers: headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (data) {
        if (!response.ok) { throw { status: response.status, body: data }; }
        return data;
      });
    }, function () { throw { status: 0, body: null }; });
  }

  function call(method, path, body, key) {
    var headers = { Accept: "application/json" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (key) {
      headers["Idempotency-Key"] = key;
    }
    return fetch(config.api + path, {
      method: method,
      headers: headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (data) {
        if (!response.ok) {
          throw { status: response.status, body: data };
        }
        publish(data);
        return data;
      });
    }, function () {
      throw { status: 0, body: null }; // network error
    });
  }

  function message(error) {
    return text(core.errorKey(error && error.status, error && error.body));
  }

  // Every part of the page shows the same cart: each answer is broadcast.
  function publish(cart) {
    window.dispatchEvent(new CustomEvent(CART_EVENT, { detail: cart }));
  }

  function useCart() {
    var state = useState(null);
    useEffect(function () {
      function onCart(event) { state[1](event.detail); }
      window.addEventListener(CART_EVENT, onCart);
      return function () { window.removeEventListener(CART_EVENT, onCart); };
    }, []);
    return state[0];
  }

  // ------------------------------------------------------------------------ header count

  function showCount(cart) {
    document.querySelectorAll("[data-cart-count]").forEach(function (element) {
      element.textContent = cart.item_count ? String(cart.item_count) : "";
      element.hidden = !cart.item_count;
    });
  }
  window.addEventListener(CART_EVENT, function (event) { showCount(event.detail); });

  // ------------------------------------------------------------------ product page: add

  function QuantityInput(props) {
    return h("input", {
      type: "number", min: 1, max: core.MAX_QUANTITY, step: 1, inputMode: "numeric", id: props.id,
      className: "cart-qty-input", value: props.value, disabled: props.disabled,
      onChange: function (event) { props.onChange(event.target.value); },
    });
  }

  function AddToCart(props) {
    var quantity = useState("1");
    var status = useState({ kind: "idle", text: "" });
    var busy = useState(false);
    var guard = useRef(null);
    var pendingKey = useRef(null);
    if (!guard.current) {
      guard.current = core.createActionGuard(function () { busy[1](guard.current.isRunning("add")); });
    }
    if (!props.inStock) {
      return h("div", { className: "cart-add" }, h("p", { className: "cart-out" }, text("out_of_stock")));
    }
    function add(event) {
      event.preventDefault();
      guard.current.run("add", function () {
        // One key per click; kept if that same click has to be retried after a network error.
        pendingKey.current = pendingKey.current || core.newKey();
        var amount = core.clampQuantity(quantity[0], core.MAX_QUANTITY);
        return call("POST", "/lines", { product_id: props.productId, quantity: amount }, pendingKey.current)
          .then(function () {
            pendingKey.current = null;
            status[1]({ kind: "ok", text: text("added") });
          }, function (error) {
            if (error.status !== 0) {
              pendingKey.current = null; // refused: a new click is a new request
            }
            status[1]({ kind: "error", text: message(error) });
          });
      });
    }
    return h("form", { className: "cart-add", onSubmit: add, noValidate: true },
      h("label", { htmlFor: "cart-add-qty" }, text("quantity")),
      h(QuantityInput, { id: "cart-add-qty", value: quantity[0], onChange: quantity[1], disabled: busy[0] }),
      h("button", { type: "submit", disabled: busy[0], "aria-busy": busy[0] ? "true" : "false" },
        busy[0] ? text("adding") : text("add")),
      status[0].text ? h("p", { className: "cart-status cart-status-" + status[0].kind, role: "status" },
        status[0].text, " ",
        status[0].kind === "ok" ? h("a", { href: config.cart_url }, text("view_cart")) : null) : null
    );
  }

  // ------------------------------------------------------------------------ cart page

  function LineRow(props) {
    var line = props.line;
    var shown = props.quantities[line.id] !== undefined ? props.quantities[line.id] : line.quantity;
    var removing = props.guard.isRunning("remove:" + line.id);
    var saving = props.saving[line.id];
    return h("tr", { className: line.available ? "" : "cart-line-unavailable", "data-line": line.id },
      h("td", { className: "cart-pic" },
        line.image_url
          ? h("img", { src: line.image_url, alt: "", width: 56, height: 56, loading: "lazy" })
          : h("span", { className: "cart-no-pic" }, text("no_image"))),
      h("td", { className: "cart-name" },
        h("a", { href: line.url }, core.translated(line.name_translations, config.lang, line.name)),
        line.sku ? h("div", { className: "cart-sku" }, "SKU ", h("code", null, line.sku)) : null,
        line.price_changed ? h("div", { className: "cart-repriced" },
          text("repriced", { price: line.previous_unit_price_display })) : null,
        line.issue ? h("div", { className: "cart-warning" }, text("issue." + line.issue)) : null,
        props.errors[line.id] ? h("div", { className: "cart-error", role: "alert" }, props.errors[line.id]) : null),
      // data-label: the column's name, shown next to the value when the row is a card (phones, cart.css).
      h("td", { className: "cart-num cart-price", "data-label": text("price") }, line.unit_price_display),
      h("td", { className: "cart-qty", "data-label": text("quantity") },
        h("button", { type: "button", className: "cart-step", "aria-label": text("decrease"), disabled: shown <= 1 || removing,
                      onClick: function () { props.onStep(line, -1); } }, "\u2212"),
        h("output", { "aria-live": "polite", className: saving ? "cart-saving" : "" }, shown),
        h("button", { type: "button", className: "cart-step", "aria-label": text("increase"),
                      disabled: shown >= core.MAX_QUANTITY || removing,
                      onClick: function () { props.onStep(line, 1); } }, "+")),
      h("td", { className: "cart-num cart-total", "data-label": text("line_total") }, line.line_total_display),
      h("td", { className: "cart-remove-cell" },
        h("button", { type: "button", className: "cart-remove", disabled: removing,
                      onClick: function () { props.onRemove(line); } }, removing ? text("removing") : text("remove")))
    );
  }

  function AddByReference(props) {
    var kind = useState("sku");
    var reference = useState("");
    var quantity = useState("1");
    var busy = props.guard.isRunning("add-form");
    var error = useState("");
    var pendingKey = useRef(null);
    function submit(event) {
      event.preventDefault();
      var ref = core.productRef(kind[0], reference[0]);
      if (!ref) {
        error[1](text("error.invalid"));
        return;
      }
      props.guard.run("add-form", function () {
        pendingKey.current = pendingKey.current || core.newKey();
        ref.quantity = core.clampQuantity(quantity[0], core.MAX_QUANTITY);
        return call("POST", "/lines", ref, pendingKey.current).then(function () {
          pendingKey.current = null;
          error[1]("");
          reference[1]("");
          quantity[1]("1");
        }, function (failure) {
          if (failure.status !== 0) {
            pendingKey.current = null;
          }
          error[1](message(failure));
        });
      });
    }
    return h("form", { className: "cart-add-ref", onSubmit: submit, noValidate: true },
      h("h2", null, text("add_heading")),
      h("div", { className: "cart-add-fields" },
        h("label", { htmlFor: "cart-ref-kind" }, text("reference_kind")),
        h("select", { id: "cart-ref-kind", value: kind[0], onChange: function (e) { kind[1](e.target.value); } },
          h("option", { value: "sku" }, text("by_sku")),
          h("option", { value: "id" }, text("by_id"))),
        h("label", { htmlFor: "cart-ref" }, text("reference")),
        h("input", { id: "cart-ref", value: reference[0], autoComplete: "off", spellCheck: false,
                     onChange: function (e) { reference[1](e.target.value); } }),
        h("label", { htmlFor: "cart-ref-qty" }, text("quantity")),
        h(QuantityInput, { id: "cart-ref-qty", value: quantity[0], onChange: quantity[1], disabled: busy }),
        h("button", { type: "submit", disabled: busy, "aria-busy": busy ? "true" : "false" },
          busy ? text("adding") : text("add"))),
      error[0] ? h("p", { className: "cart-error", role: "alert" }, error[0]) : null
    );
  }

  // "Empty the cart": a second click confirms it, so a rage click can't empty it by accident.
  function ClearCart(props) {
    var asking = useState(false);
    var busy = props.guard.isRunning("clear");
    if (!asking[0]) {
      return h("button", { type: "button", className: "cart-clear", onClick: function () { asking[1](true); } },
        text("clear"));
    }
    return h("span", { className: "cart-clear-confirm", role: "group" },
      text("clear_confirm"), " ",
      h("button", { type: "button", className: "cart-clear-yes", disabled: busy, onClick: function () {
        props.guard.run("clear", function () {
          return call("DELETE", "").then(function () { asking[1](false); }, function (error) {
            asking[1](false);
            props.onError(message(error));
          });
        });
      } }, busy ? text("clearing") : text("clear_yes")), " ",
      h("button", { type: "button", disabled: busy, onClick: function () { asking[1](false); } }, text("clear_no")));
  }

  // Checkout: the shipping method and the payment terminal are chosen here, separately (the cart
  // holds neither). The shipping cost shown is the store's quote API; the server quotes again and
  // computes the total itself when the order is created.
  function Checkout(props) {
    var cart = props.cart;
    var options = useState(null); // {methods, terminals}
    var method = useState("");
    var country = useState("");
    var terminal = useState("");
    var quote = useState({ state: "idle" });
    var error = useState("");
    var latestQuote = useRef(0);
    var pendingKey = useRef(null);
    var busy = props.guard.isRunning("checkout");

    useEffect(function () {
      Promise.all([fetchJson("GET", config.store_api + "/shipping/methods"),
                   fetchJson("GET", config.store_api + "/payment/terminals")]).then(function (found) {
        var terminals = found[1].filter(function (t) { return t.enabled; });
        options[1]({ methods: found[0], terminals: terminals });
        if (found[0].length) {
          method[1](found[0][0].code);
          if ((found[0][0].countries || []).length === 1) { country[1](found[0][0].countries[0]); }
        }
        if (terminals.length) { terminal[1](terminals[0].code); }
      }, function () { error[1](text("error.generic")); });
    }, []);

    // Quote again whenever the method, the country or the cart's subtotal change; a late answer
    // to an older question is ignored.
    useEffect(function () {
      if (!method[0]) { return; }
      var ask = ++latestQuote.current;
      quote[1]({ state: "loading" });
      var body = { method: method[0], subtotal_cents: cart.subtotal_cents };
      var code = country[0].trim().toUpperCase();
      if (/^[A-Z]{2}$/.test(code)) { body.country = code; }
      fetchJson("POST", config.store_api + "/shipping/quote", body).then(function (answer) {
        if (ask === latestQuote.current) { quote[1]({ state: "ok", shipping: answer.amount_cents, free: answer.free_shipping }); }
      }, function (failure) {
        if (ask === latestQuote.current) { quote[1]({ state: "error", text: message(failure) }); }
      });
    }, [method[0], country[0], cart.subtotal_cents]);

    if (!options[0]) {
      return h("section", { className: "cart-checkout" }, h("p", { className: "meta" }, error[0] || text("loading")));
    }
    if (!options[0].methods.length || !options[0].terminals.length) {
      return h("section", { className: "cart-checkout" }, h("h2", null, text("checkout.heading")),
        h("p", { className: "meta" }, !options[0].methods.length ? text("checkout.no_methods") : text("checkout.no_terminals")));
    }
    var total = quote[0].state === "ok" ? cart.subtotal_cents + quote[0].shipping : null;
    var chosen = options[0].terminals.filter(function (t) { return t.code === terminal[0]; })[0];
    var byTransfer = chosen && chosen.kind === "bank_transfer"; // offline: pay later, the store confirms
    function pay(event) {
      event.preventDefault();
      var body = core.checkoutBody(method[0], country[0], terminal[0]);
      if (!body || total === null) { return; }
      props.guard.run("checkout", function () {
        pendingKey.current = pendingKey.current || core.newKey(); // one key per click, kept for its retries
        return fetchJson("POST", config.api + "/checkout", body, pendingKey.current).then(function (result) {
          pendingKey.current = null;
          error[1]("");
          props.onOrdered(result);
        }, function (failure) {
          if (failure.status !== 0) { pendingKey.current = null; }
          error[1](message(failure));
          call("GET", "").catch(function () {}); // show the lines' new prices / problems
        });
      });
    }
    var payTotal = total === null ? "" : core.formatMoney(total, cart.currency);
    return h("section", { className: "cart-checkout" },
      h("h2", null, text("checkout.heading")),
      h("form", { className: "cart-checkout-form", onSubmit: pay, noValidate: true },
        h("label", { htmlFor: "checkout-method" }, text("checkout.shipping")),
        h("select", { id: "checkout-method", value: method[0], disabled: busy,
                      onChange: function (e) { method[1](e.target.value); } },
          options[0].methods.map(function (m) { return h("option", { key: m.code, value: m.code }, nameOf("shipping", m)); })),
        h("label", { htmlFor: "checkout-country" }, text("checkout.country")),
        h("input", { id: "checkout-country", value: country[0], maxLength: 2, size: 3, disabled: busy, autoComplete: "country",
                     onChange: function (e) { country[1](e.target.value); } }),
        h("label", { htmlFor: "checkout-terminal" }, text("checkout.terminal")),
        h("select", { id: "checkout-terminal", value: terminal[0], disabled: busy,
                      onChange: function (e) { terminal[1](e.target.value); } },
          options[0].terminals.map(function (t) { return h("option", { key: t.code, value: t.code }, nameOf("terminals", t)); })),
        h("dl", { className: "cart-checkout-sum" },
          h("dt", null, text("subtotal")), h("dd", null, cart.subtotal_display),
          h("dt", null, text("checkout.shipping_cost")),
          h("dd", null, quote[0].state === "ok" ? (quote[0].free ? text("checkout.free") : core.formatMoney(quote[0].shipping, cart.currency))
                       : quote[0].state === "error" ? h("span", { className: "cart-error" }, quote[0].text) : text("checkout.quoting")),
          h("dt", null, text("checkout.total")), h("dd", null, h("strong", null, payTotal || "—"))),
        byTransfer ? h("p", { className: "cart-transfer-hint" }, text("checkout.transfer_hint")) : null,
        h("button", { type: "submit", className: "cart-pay", disabled: busy || total === null, "aria-busy": busy ? "true" : "false" },
          busy ? text("checkout.paying") : text(byTransfer ? "checkout.pay_transfer" : "checkout.pay", { total: payTotal }))),
      error[0] ? h("p", { className: "cart-error", role: "alert" }, error[0]) : null);
  }

  // Where and how to deposit a bank transfer (the values to copy into the banking app).
  function TransferInstructions(props) {
    var i = props.instructions;
    var note = core.translated(i.note_translations, config.lang, i.note); // in the page language when translated
    var rows = [["bank.amount", i.amount_display], ["bank.bank", i.bank], ["bank.beneficiary", i.beneficiary],
                ["bank.clabe", i.clabe], ["bank.account", i.account], ["bank.reference", i.reference],
                ["bank.concept", i.concept]].filter(function (row) { return row[1]; });
    return h("div", { className: "order-transfer" },
      h("h3", null, text("order.transfer_heading")),
      h("p", null, text("order.transfer_intro", { amount: i.amount_display })),
      h("dl", { className: "order-transfer-details" }, rows.map(function (row) {
        return [h("dt", { key: row[0] + "t" }, text(row[0])),
                h("dd", { key: row[0] + "d" }, h("code", { className: row[0] === "bank.reference" ? "order-reference" : "" }, row[1]))];
      })),
      note ? h("p", { className: "order-transfer-note" }, note) : null);
  }

  function OrderDone(props) {
    var order = props.result.order;
    var payment = props.result.payment;
    return h("section", { className: "cart-order-done", role: "status" },
      h("h2", null, text("checkout.done_heading")),
      h("p", null, text("checkout.done", { order: order.id, total: order.total_display })),
      payment.instructions
        ? h(TransferInstructions, { instructions: payment.instructions })
        : h("p", null, text("checkout.payment", { payment: payment.id, status: text("payment_status." + payment.status) })),
      h("p", null, h("a", { href: config.store_url + "/orders/" + order.id }, text("checkout.order_link"))),
      h("p", { className: "meta" }, text("checkout.cart_emptied")));
  }

  function CartApp() {
    var cart = useCart();
    var failed = useState(false);
    var quantities = useState({}); // line id -> number shown while its change waits / is sent
    var saving = useState({});
    var errors = useState({});
    var tick = useState(0);
    var cartError = useState("");
    var ordered = useState(null); // the last checkout's answer, shown above the (now empty) cart
    var guard = useRef(null);
    var batcher = useRef(null);
    // The latest number asked for each line, updated at once by every click (not at the next
    // render), so clicks faster than React renders all count.
    var latest = useRef({});
    if (!guard.current) {
      guard.current = core.createActionGuard(function () { tick[1](function (n) { return n + 1; }); });
      batcher.current = core.createBatcher(function (id, value) {
        saving[1](function (s) { var next = Object.assign({}, s); next[id] = true; return next; });
        return call("PATCH", "/lines/" + id, { quantity: value }).finally(function () {
          saving[1](function (s) { var next = Object.assign({}, s); delete next[id]; return next; });
        });
      }, core.QUANTITY_DELAY_MS);
    }
    useEffect(function () {
      call("GET", "").catch(function () { failed[1](true); });
    }, []);

    function setFor(setter, id, value) {
      setter(function (current) {
        var next = Object.assign({}, current);
        if (value === undefined) { delete next[id]; } else { next[id] = value; }
        return next;
      });
    }
    function onStep(line, delta) {
      var current = latest.current[line.id] !== undefined ? latest.current[line.id] : line.quantity;
      var value = core.clampQuantity(current + delta, core.MAX_QUANTITY);
      latest.current[line.id] = value;
      setFor(quantities[1], line.id, value); // shown at once; sent when the clicks stop
      setFor(errors[1], line.id, undefined);
      batcher.current.push(line.id, value).then(function () {
        if (!batcher.current.isPending(line.id)) {
          delete latest.current[line.id];
          setFor(quantities[1], line.id, undefined);
        }
      }, function (error) {
        delete latest.current[line.id];
        setFor(quantities[1], line.id, undefined); // back to the cart's number
        setFor(errors[1], line.id, message(error));
      });
    }
    function onRemove(line) {
      guard.current.run("remove:" + line.id, function () {
        return call("DELETE", "/lines/" + line.id).catch(function (error) {
          setFor(errors[1], line.id, message(error));
        });
      });
    }

    if (failed[0] && !cart) {
      return h("p", { className: "cart-error", role: "alert" }, text("error.generic"));
    }
    if (!cart) {
      return h("p", { className: "meta" }, text("loading"));
    }
    var add = h(AddByReference, { guard: guard.current });
    var done = ordered[0] ? h(OrderDone, { result: ordered[0] }) : null;
    if (!cart.lines.length) {
      return h("div", { className: "cart-page" },
        done,
        h("p", { className: "cart-empty" }, text("empty")),
        h("p", null, h("a", { href: config.store_url }, text("continue"))),
        add);
    }
    return h("div", { className: "cart-page" },
      done,
      h("table", { className: "cart-lines" },
        h("thead", null, h("tr", null,
          h("th", null, h("span", { className: "visually-hidden" }, text("picture"))),
          h("th", null, text("product")), h("th", null, text("price")), h("th", null, text("quantity")),
          h("th", null, text("line_total")), h("th", null, h("span", { className: "visually-hidden" }, text("remove"))))),
        h("tbody", null, cart.lines.map(function (line) {
          return h(LineRow, { key: line.id, line: line, quantities: quantities[0], saving: saving[0], errors: errors[0],
                              guard: guard.current, onStep: onStep, onRemove: onRemove });
        }))),
      h("p", { className: "cart-subtotal" },
        text("items", { count: cart.item_count }), " · ", text("subtotal"), ": ", h("strong", null, cart.subtotal_display)),
      h("div", { className: "cart-actions" }, h(ClearCart, { guard: guard.current, onError: cartError[1] })),
      cartError[0] ? h("p", { className: "cart-error", role: "alert" }, cartError[0]) : null,
      h(Checkout, { cart: cart, guard: guard.current, onOrdered: function (result) {
        ordered[1](result);
        call("GET", "").catch(function () {}); // the cart is empty now: refresh it (and the header count)
      } }),
      h("p", null, h("a", { href: config.store_url }, text("continue"))),
      add);
  }

  // ----------------------------------------------------------------------------- start

  var addRoot = document.getElementById("cart-add");
  if (addRoot) {
    ReactDOM.createRoot(addRoot).render(h(AddToCart, {
      productId: parseInt(addRoot.getAttribute("data-product-id"), 10),
      inStock: addRoot.getAttribute("data-in-stock") === "true",
    }));
  }
  var appRoot = document.getElementById("cart-app");
  if (appRoot) {
    ReactDOM.createRoot(appRoot).render(h(CartApp));
  } else {
    call("GET", "").catch(function () { /* the count is optional */ });
  }
})();
