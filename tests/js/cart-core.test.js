// Node's built-in test runner (node --test): the cart's logic without a browser. Run by
// tests/test_cart_js.py (pytest), or directly: node --test tests/js/cart-core.test.js
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../../app/static/cart/cart-core.js");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

test("a rage click on a running action does nothing", async () => {
  const guard = core.createActionGuard();
  const gate = deferred();
  let calls = 0;
  const first = guard.run("add", () => { calls += 1; return gate.promise; });
  assert.equal(guard.isRunning("add"), true);
  const clicks = [];
  for (let i = 0; i < 9; i += 1) {
    clicks.push(guard.run("add", () => { calls += 1; }));
  }
  for (const result of await Promise.all(clicks)) {
    assert.deepEqual(result, { skipped: true });
  }
  assert.equal(calls, 1);
  gate.resolve("done");
  assert.equal(await first, "done");
  assert.equal(guard.isRunning("add"), false);
  await guard.run("add", () => { calls += 1; }); // a click after the answer runs again
  assert.equal(calls, 2);
});

test("actions with different names don't block each other, and failures free the guard", async () => {
  let changes = 0;
  const guard = core.createActionGuard(() => { changes += 1; });
  const gate = deferred();
  const removeOne = guard.run("remove:1", () => gate.promise);
  let ran = false;
  await guard.run("remove:2", () => { ran = true; });
  assert.equal(ran, true);
  gate.reject(new Error("network"));
  await assert.rejects(removeOne, /network/);
  assert.equal(guard.isRunning("remove:1"), false);
  assert.ok(changes >= 4); // start + end of each action
});

test("quick quantity clicks become one request with the last number", async () => {
  const timers = [];
  const fakeTimers = {
    set(fn) { timers.push(fn); return timers.length - 1; },
    clear(id) { timers[id] = null; },
  };
  const sent = [];
  const batcher = core.createBatcher((id, value) => { sent.push([id, value]); return Promise.resolve({ id, value }); },
                                     400, fakeTimers);
  const promises = [2, 3, 4, 5].map((value) => batcher.push(7, value));
  assert.equal(batcher.isPending(7), true);
  assert.equal(timers.filter(Boolean).length, 1); // each click restarted the timer
  timers.filter(Boolean)[0]();
  const results = await Promise.all(promises);
  assert.deepEqual(sent, [[7, 5]]);
  for (const result of results) {
    assert.deepEqual(result, { id: 7, value: 5 });
  }
  assert.equal(batcher.isPending(7), false);
});

test("each line has its own batch, and a refusal reaches every click of it", async () => {
  const timers = [];
  const batcher = core.createBatcher((id) => (id === 1 ? Promise.reject({ status: 409 }) : Promise.resolve("ok")), 10,
                                     { set(fn) { timers.push(fn); return timers.length - 1; }, clear(i) { timers[i] = null; } });
  const a = [batcher.push(1, 3), batcher.push(1, 4)];
  const b = batcher.push(2, 9);
  timers.filter(Boolean).forEach((fn) => fn());
  for (const p of a) {
    await assert.rejects(p, (error) => error.status === 409);
  }
  assert.equal(await b, "ok");
});

test("the batcher works with real timers", async () => {
  const sent = [];
  const batcher = core.createBatcher((id, value) => { sent.push(value); return Promise.resolve(value); }, 5);
  batcher.push(1, 1);
  const last = batcher.push(1, 2);
  assert.equal(await last, 2);
  assert.deepEqual(sent, [2]);
});

test("API errors map to the page's message keys", () => {
  const refused = (type) => ({ detail: [{ loc: ["body"], msg: "x", type }] });
  assert.equal(core.errorKey(409, refused("insufficient_stock")), "error.insufficient_stock");
  assert.equal(core.errorKey(409, refused("out_of_stock")), "error.out_of_stock");
  assert.equal(core.errorKey(422, refused("unknown_product")), "error.unknown_product");
  assert.equal(core.errorKey(422, refused("quantity_limit")), "error.quantity_limit");
  assert.equal(core.errorKey(422, refused("cart_full")), "error.cart_full");
  assert.equal(core.errorKey(409, refused("request_in_progress")), "error.request_in_progress");
  assert.equal(core.errorKey(422, refused("int_parsing")), "error.invalid");
  assert.equal(core.errorKey(500, null), "error.generic");
  assert.equal(core.errorKey(0, null), "error.generic");
});

test("the add form sends a SKU or a product id", () => {
  assert.deepEqual(core.productRef("sku", "  sol-8t-alta "), { sku: "sol-8t-alta" });
  assert.deepEqual(core.productRef("sku", "12345"), { sku: "12345" }); // an all-digit SKU stays a SKU
  assert.deepEqual(core.productRef("id", "42"), { product_id: 42 });
  assert.equal(core.productRef("id", "4x"), null);
  assert.equal(core.productRef("sku", "   "), null);
  assert.equal(core.productRef("id", "9".repeat(19)), null);
});

test("quantities stay between 1 and the maximum", () => {
  assert.equal(core.clampQuantity("3", 99), 3);
  assert.equal(core.clampQuantity("0", 99), 1);
  assert.equal(core.clampQuantity("abc", 99), 1);
  assert.equal(core.clampQuantity("500", 99), 99);
});

test("texts with placeholders, and keys", () => {
  assert.equal(core.format("{count} artículo(s)", { count: 3 }), "3 artículo(s)");
  assert.equal(core.format("{missing} ok", {}), "{missing} ok");
  const keys = new Set(Array.from({ length: 50 }, () => core.newKey()));
  assert.equal(keys.size, 50);
  for (const key of keys) {
    assert.match(key, /^[A-Za-z0-9_-]{8,64}$/); // what the server accepts as an Idempotency-Key
  }
});

test("money is shown like the server shows it", () => {
  assert.equal(core.formatMoney(649900, "MXN"), "MXN 6,499.00");
  assert.equal(core.formatMoney(5, "USD"), "USD 0.05");
  assert.equal(core.formatMoney(0, "MXN"), "MXN 0.00");
  assert.equal(core.formatMoney(123456789, "EUR"), "EUR 1,234,567.89");
  assert.equal(core.formatMoney(-1050, "MXN"), "MXN -10.50");
});

test("the checkout body: method and terminal chosen separately, country only when given", () => {
  assert.deepEqual(core.checkoutBody("standard", " mx ", "mock_card"),
                   { shipping_method: "standard", terminal: "mock_card", country: "MX" });
  assert.deepEqual(core.checkoutBody("express", "", "mock_card"), { shipping_method: "express", terminal: "mock_card" });
  assert.equal(core.checkoutBody("", "MX", "mock_card"), null);
  assert.equal(core.checkoutBody("standard", "MX", ""), null);
});

test("a refused checkout names the cart lines at fault", () => {
  const body = { detail: [
    { loc: ["cart", "lines", 7], type: "insufficient_stock", msg: "x" },
    { loc: ["cart", "lines", 9], type: "unavailable", msg: "x" },
    { loc: ["body", "terminal"], type: "unknown_terminal", msg: "x" },
  ] };
  assert.deepEqual(core.refusedLines(body), [7, 9]);
  assert.deepEqual(core.refusedLines(null), []);
  assert.deepEqual(core.refusedLines({ detail: "Store not found" }), []);
});

test("checkout refusals map to the page's messages", () => {
  const refused = (type) => ({ detail: [{ loc: ["cart"], msg: "x", type }] });
  for (const type of ["cart_empty", "unavailable", "price_changed", "unknown_method", "inactive_method",
                      "country_required", "country_not_served", "unknown_terminal", "terminal_disabled",
                      "nothing_to_pay", "total_too_large", "terminal_not_configured"]) {
    assert.equal(core.errorKey(409, refused(type)), "error." + type);
  }
});

test("names and notes in the page's language, else as entered", () => {
  assert.equal(core.translated({ en: "Bank transfer" }, "en", "Transferencia bancaria"), "Bank transfer");
  assert.equal(core.translated({ en: "Bank transfer" }, "es", "Transferencia bancaria"), "Transferencia bancaria");
  assert.equal(core.translated({ en: "" }, "en", "Nota"), "Nota"); // an empty translation is no translation
  assert.equal(core.translated(undefined, "en", "Nota"), "Nota"); // older answers have none
  assert.equal(core.translated({ toString: "x" }, "toString", "Nota"), "x");
  assert.equal(core.translated({}, "toString", "Nota"), "Nota"); // only its own keys
  const names = { shipping: { standard: "Standard shipping" }, terminals: { bank_transfer: "Bank transfer" } };
  assert.equal(core.nameOf(names, "shipping", { code: "standard", name: "Envío estándar" }), "Standard shipping");
  assert.equal(core.nameOf(names, "terminals", { code: "mock_card", name: "Mock card" }), "Mock card");
  assert.equal(core.nameOf(undefined, "terminals", { code: "bank_transfer", name: "Transferencia" }), "Transferencia");
});
