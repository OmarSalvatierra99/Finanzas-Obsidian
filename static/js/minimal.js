(() => {
    const open = id => {
        const dialog = document.getElementById(id);
        if (!dialog) return;
        document.querySelectorAll('dialog[open]').forEach(d => d.close());
        dialog.showModal();
    };
    document.addEventListener('click', event => {
        const trigger = event.target.closest('[data-open]');
        if (trigger) open(trigger.dataset.open);
        const close = event.target.closest('[data-close-dialog]');
        if (close) close.closest('dialog').close();
        const newDate = event.target.closest('[data-new-date]');
        if (newDate) {
            open('new-debt');
            const form = document.querySelector('#new-debt form');
            form.elements.month.value = newDate.dataset.newDate.slice(0, 7);
            form.querySelectorAll('input[type=date]').forEach(input => input.value = newDate.dataset.newDate);
        }
        const day = event.target.closest('.minimal-calendar .month-day');
        if (day && !event.target.closest('button, form') && matchMedia('(max-width:760px)').matches) open('day-' + day.dataset.dropDate);
    });
    document.querySelectorAll('[data-confirm]').forEach(form => form.addEventListener('submit', event => {
        if (!confirm(form.dataset.confirm)) event.preventDefault();
    }));
    document.querySelectorAll('[data-recurring-frequency]').forEach(select => {
        const update = () => {
            const form = select.closest('form');
            ['weekly', 'monthly', 'fortnightly'].forEach(f => {
                const group = form.querySelector(`[data-${f}-calendar]`);
                if (!group) return;
                group.hidden = select.value !== f;
                group.querySelectorAll('input,select').forEach(input => input.disabled = group.hidden);
            });
        };
        select.addEventListener('change', update); update();
    });
    document.querySelectorAll('[data-simple-frequency]').forEach(select => {
        const update = () => select.closest('form').querySelector('[data-fortnight-note]').hidden = select.value !== 'fortnightly';
        select.addEventListener('change', update); update();
    });
    const toast = message => {
        const box = document.createElement('div');
        box.className = 'minimal-toast'; box.setAttribute('role', 'status'); box.textContent = message;
        document.body.append(box);
    };
    sessionStorage.removeItem('finance-undo');
    document.querySelectorAll('form[data-complete]').forEach(form => form.addEventListener('submit', async event => {
        event.preventDefault();
        const button = form.querySelector('button'); if (button.disabled) return;
        button.disabled = true;
        const data = new FormData(form);
        try {
            const response = await fetch(form.action, {method:'POST', body:data, headers:{Accept:'application/json'}});
            const result = await response.json();
            if (!response.ok || !result.ok) throw Error(result.message);
            location.reload();
        } catch (error) { toast(error.message || 'No se pudo guardar.'); button.disabled = false; }
    }));
    const openHash = () => {
        const row = document.getElementById(location.hash.slice(1));
        const button = row?.querySelector('[data-open]');
        if (button) open(button.dataset.open);
    };
    addEventListener('hashchange', openHash); openHash();
})();
