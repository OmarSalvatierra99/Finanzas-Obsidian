/* Prevent repeated clicks while a financial form is being submitted. */
document.addEventListener('submit', event => {
    const form = event.target;
    if (!form.matches('[data-single-submit]') || event.defaultPrevented) return;
    if (form.dataset.submitting) { event.preventDefault(); return; }
    form.dataset.submitting = '1';
    const button = event.submitter;
    if (button) setTimeout(() => { button.disabled = true; button.dataset.submitDisabled = '1'; }, 0);
});
window.addEventListener('pageshow', () => {
    document.querySelectorAll('[data-single-submit]').forEach(form => {
        delete form.dataset.submitting;
        form.querySelectorAll('[data-submit-disabled]').forEach(button => {
            button.disabled = false; delete button.dataset.submitDisabled;
        });
    });
});
