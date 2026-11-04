const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

test("blocks repeated payment submit events and disables the first submit button", () => {
    const handlers = {};
    const form = {
        dataset: {},
        addEventListener(type, handler) {
            handlers[type] = handler;
        },
        querySelector() {
            return button;
        },
    };
    const button = { disabled: false, textContent: "Pay" };
    const document = {
        querySelectorAll(selector) {
            assert.equal(selector, "form[data-payment-submit]");
            return [form];
        },
    };
    const source = fs.readFileSync("assets/js/payment-submit.js", "utf8");
    vm.runInNewContext(source, { document });

    let firstPrevented = false;
    handlers.submit({ preventDefault() { firstPrevented = true; } });
    assert.equal(firstPrevented, false);
    assert.equal(form.dataset.paymentSubmitting, "true");
    assert.equal(button.disabled, true);

    let secondPrevented = false;
    handlers.submit({ preventDefault() { secondPrevented = true; } });
    assert.equal(secondPrevented, true);
});
