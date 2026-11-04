document.querySelectorAll("form[data-payment-submit]").forEach((form) => {
    form.addEventListener("submit", (event) => {
        if (form.dataset.paymentSubmitting === "true") {
            event.preventDefault();
            return;
        }

        form.dataset.paymentSubmitting = "true";
        const submitButton = form.querySelector('button[type="submit"]');
        if (submitButton) {
            submitButton.disabled = true;
            submitButton.textContent = "Opening Paystack…";
        }
    });
});
