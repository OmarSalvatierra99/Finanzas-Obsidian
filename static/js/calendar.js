/* Pointer events support mouse, pen and touch without changing page scrolling. */
(() => {
    const calendar = document.querySelector('.month-calendar');
    if (!calendar) return;
    const status = document.createElement('p');
    status.className = 'calendar-feedback';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    calendar.closest('.month-calendar-wrap').before(status);
    let drag = null;
    let busy = false;
    let frame = null;

    function clearDrag() {
        if (!drag) return;
        drag.card.classList.remove('is-dragging');
        drag.target?.classList.remove('drop-target');
        drag.ghost?.remove();
        if (drag.handle.hasPointerCapture(drag.id)) drag.handle.releasePointerCapture(drag.id);
        drag = null;
        cancelAnimationFrame(frame);
    }

    function updateTarget() {
        if (!drag?.active) return;
        drag.target?.classList.remove('drop-target');
        drag.target = document.elementFromPoint(drag.x, drag.y)?.closest('[data-drop-date]');
        if (drag.target && !calendar.contains(drag.target)) drag.target = null;
        drag.target?.classList.add('drop-target');
        drag.ghost.style.transform = `translate(${drag.x + 12}px, ${drag.y + 12}px)`;
    }

    function scrollEdges() {
        if (!drag?.active) return;
        const wrap = calendar.closest('.month-calendar-wrap');
        const bounds = wrap.getBoundingClientRect();
        if (drag.x < bounds.left + 36) wrap.scrollLeft -= 8;
        if (drag.x > bounds.right - 36) wrap.scrollLeft += 8;
        if (drag.y < 64) window.scrollBy(0, -8);
        if (drag.y > window.innerHeight - 64) window.scrollBy(0, 8);
        updateTarget();
        frame = requestAnimationFrame(scrollEdges);
    }

    async function saveMove(form, target) {
        if (busy) return;
        busy = true;
        const data = new FormData(form);
        data.set('target_on', target);
        if (data.has('due_on')) data.set('due_on', target);
        status.textContent = 'Guardando nueva fecha…';
        calendar.setAttribute('aria-busy', 'true');
        const buttons = [...calendar.querySelectorAll('button')];
        const disabled = buttons.map(button => button.disabled);
        buttons.forEach(button => { button.disabled = true; });
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 20000);
        try {
            const response = await fetch(form.action, {
                method: 'POST', body: data, headers: {Accept: 'application/json'},
                credentials: 'same-origin', signal: controller.signal
            });
            if (!response.headers.get('content-type')?.includes('application/json')) {
                throw new Error('La sesión expiró o la solicitud no es válida. Recarga el calendario.');
            }
            const result = await response.json();
            if (!response.ok || !result.ok) throw new Error(result.message || 'No se pudo mover el pago.');
            status.textContent = result.message;
            const url = new URL(location.href);
            url.searchParams.set('month', target.slice(0, 7));
            location.assign(url.href);
        } catch (error) {
            status.textContent = error.name === 'AbortError'
                ? 'No se pudo confirmar el cambio. Recarga el calendario para verificar la fecha.'
                : error.message;
            busy = false;
            calendar.removeAttribute('aria-busy');
            buttons.forEach((button, index) => { button.disabled = disabled[index]; });
        } finally { clearTimeout(timeout); }
    }

    calendar.addEventListener('pointerdown', event => {
        const handle = event.target.closest('.payment-drag-handle');
        if (!handle || busy || drag || event.button !== 0) return;
        const card = handle.closest('[data-payment-card]');
        const form = card.querySelector('.calendar-move-form');
        if (!form) return;
        drag = {handle, card, form, id: event.pointerId, startX: event.clientX,
                startY: event.clientY, x: event.clientX, y: event.clientY, active: false};
        handle.setPointerCapture(event.pointerId);
    });
    calendar.addEventListener('pointermove', event => {
        if (!drag || drag.id !== event.pointerId) return;
        drag.x = event.clientX; drag.y = event.clientY;
        if (!drag.active && Math.hypot(drag.x - drag.startX, drag.y - drag.startY) > 6) {
            drag.active = true;
            drag.card.classList.add('is-dragging');
            drag.ghost = document.createElement('div');
            drag.ghost.className = 'payment-drag-ghost';
            drag.ghost.textContent = drag.card.querySelector('.payment-heading strong, .minimal-row-content span')?.textContent || 'Pago';
            document.body.append(drag.ghost);
            status.textContent = 'Suelta el pago en su nueva fecha. Escape cancela.';
            scrollEdges();
        }
        if (drag.active) { event.preventDefault(); updateTarget(); }
    });
    calendar.addEventListener('pointerup', event => {
        if (!drag || drag.id !== event.pointerId) return;
        const {active, target, form} = drag;
        clearDrag();
        if (active && target) {
            if (target.dataset.dropDate !== form.elements.expected_on.value) saveMove(form, target.dataset.dropDate);
            else status.textContent = 'El pago conserva su fecha.';
        } else if (active) status.textContent = 'Movimiento cancelado.';
    });
    calendar.addEventListener('pointercancel', () => { clearDrag(); status.textContent = 'Movimiento cancelado.'; });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && drag) { clearDrag(); status.textContent = 'Movimiento cancelado.'; }
    });
    calendar.addEventListener('click', event => {
        const handle = event.target.closest('.payment-drag-handle');
        if (!handle || event.detail !== 0) return;
        const card = handle.closest('[data-payment-card]');
        const edit = card.querySelector('[data-open]');
        if (edit) { edit.click(); return; }
        const form = card.querySelector('.calendar-move-form');
        for (let parent = form.parentElement; parent; parent = parent.parentElement) {
            if (parent.tagName === 'DETAILS') parent.open = true;
        }
        form.elements.target_on.focus();
    });
    calendar.addEventListener('submit', event => {
        if (busy) { event.preventDefault(); return; }
        if (!event.target.matches('.calendar-move-form')) return;
        event.preventDefault();
        saveMove(event.target, event.target.elements.target_on.value);
    });
})();
