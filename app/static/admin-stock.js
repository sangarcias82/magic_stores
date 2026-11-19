// Stock adjustment stepper (admin product list and product page). Progressive enhancement: the
// field is a plain <input type="number"> that works without this file; this adds − / + buttons
// around every input[data-stepper]. The buttons only change the number: saving is the form's own
// Save button (with its reason).
(function () {
  "use strict";
  function stepper(input) {
    var minus = document.createElement("button");
    var plus = document.createElement("button");
    minus.type = plus.type = "button";
    minus.className = plus.className = "stepper-button";
    minus.textContent = "\u2212";
    plus.textContent = "+";
    minus.setAttribute("aria-label", input.getAttribute("data-minus-label") || "-1");
    plus.setAttribute("aria-label", input.getAttribute("data-plus-label") || "+1");
    function step(by) {
      var value = parseInt(input.value, 10);
      value = (isNaN(value) ? 0 : value) + by;
      input.value = String(value);  // a number input accepts no "+" sign
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.focus();
    }
    minus.addEventListener("click", function () { step(-1); });
    plus.addEventListener("click", function () { step(1); });
    input.parentNode.insertBefore(minus, input);
    input.parentNode.insertBefore(plus, input.nextSibling);
  }
  document.querySelectorAll("input[data-stepper]").forEach(stepper);
})();
