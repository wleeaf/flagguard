/* Admin panel client-side glue for HTMX navigation.
 *
 * The panel uses `hx-boost` for page navigation, which means `DOMContentLoaded`
 * only fires once. This file re-initializes interactive widgets on HTMX swaps.
 *
 * IMPORTANT: All interactive behavior uses event delegation on `document` so it
 * survives HTMX body swaps. No inline onclick handlers — CSP nonces block them.
 */

(function () {
    'use strict';

    // ──────────────────────────────────────────────
    // Utility helpers
    // ──────────────────────────────────────────────

    function _safeJsonParse(text) {
        try {
            return JSON.parse(text);
        } catch (_err) {
            return null;
        }
    }

    function _loadJsonFromScriptTag(id) {
        var el = document.getElementById(id);
        if (!el) return null;
        return _safeJsonParse((el.textContent || '').trim());
    }

    function _escapeHtml(str) {
        var div = document.createElement('div');
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }

    function _hashString(str) {
        var hash = 0;
        for (var i = 0; i < str.length; i++) {
            hash = ((hash << 5) - hash) + str.charCodeAt(i);
            hash |= 0;
        }
        return hash;
    }

    // ──────────────────────────────────────────────
    // Generic modal show/hide (Step 2)
    // ──────────────────────────────────────────────

    function _showModal(modalId, boxId) {
        var modal = document.getElementById(modalId);
        var box = document.getElementById(boxId);
        if (!modal || !box) return;
        modal.classList.remove('hidden');
        requestAnimationFrame(function () {
            box.classList.remove('scale-95', 'opacity-0');
            box.classList.add('scale-100', 'opacity-100');
        });
        _trapFocus(modal);
    }

    function _hideModal(modalId, boxId) {
        var modal = document.getElementById(modalId);
        var box = document.getElementById(boxId);
        if (!modal || !box) return;
        box.classList.remove('scale-100', 'opacity-100');
        box.classList.add('scale-95', 'opacity-0');
        _releaseFocus(modal);
        setTimeout(function () { modal.classList.add('hidden'); }, 200);
    }

    // Generic tab switch helper
    function _switchTabs(tabSelector, panelSelector, tabKey, activeClasses, inactiveClasses) {
        document.querySelectorAll(tabSelector).forEach(function (t) {
            var classes = t.dataset.tab === tabKey ? activeClasses : inactiveClasses;
            var remove = t.dataset.tab === tabKey ? inactiveClasses : activeClasses;
            for (var i = 0; i < remove.length; i++) t.classList.remove(remove[i]);
            for (var j = 0; j < classes.length; j++) t.classList.add(classes[j]);
            t.setAttribute('aria-selected', t.dataset.tab === tabKey ? 'true' : 'false');
        });
        document.querySelectorAll(panelSelector).forEach(function (p) {
            p.classList.toggle('hidden', p.dataset.panel !== tabKey);
        });
    }

    // ──────────────────────────────────────────────
    // Focus trapping (Step 6 — Accessibility)
    // ──────────────────────────────────────────────

    var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    function _trapFocus(modalElement) {
        modalElement._prevFocus = document.activeElement;
        var focusable = modalElement.querySelectorAll(FOCUSABLE);
        if (focusable.length === 0) return;
        focusable[0].focus();

        modalElement._trapHandler = function (e) {
            if (e.key !== 'Tab') return;
            var els = modalElement.querySelectorAll(FOCUSABLE);
            if (els.length === 0) return;
            var first = els[0];
            var last = els[els.length - 1];
            if (e.shiftKey) {
                if (document.activeElement === first) {
                    e.preventDefault();
                    last.focus();
                }
            } else {
                if (document.activeElement === last) {
                    e.preventDefault();
                    first.focus();
                }
            }
        };
        document.addEventListener('keydown', modalElement._trapHandler);
    }

    function _releaseFocus(modalElement) {
        if (modalElement._trapHandler) {
            document.removeEventListener('keydown', modalElement._trapHandler);
            modalElement._trapHandler = null;
        }
        if (modalElement._prevFocus && typeof modalElement._prevFocus.focus === 'function') {
            modalElement._prevFocus.focus();
        }
        modalElement._prevFocus = null;
    }

    // ──────────────────────────────────────────────
    // Charts with caching (Step 5)
    // ──────────────────────────────────────────────

    var _chartCache = {};

    function _setChartDefaults() {
        if (typeof Chart === 'undefined') return;
        if (Chart.defaults && Chart.defaults.font) {
            Chart.defaults.font.family = 'ui-sans-serif, system-ui, sans-serif';
        }
    }

    function initRequestsChart() {
        var canvas = document.getElementById('requestsChart');
        if (!canvas) {
            // Navigated away — destroy cached instance
            if (_chartCache.requests) {
                if (_chartCache.requests.instance && typeof _chartCache.requests.instance.destroy === 'function') {
                    _chartCache.requests.instance.destroy();
                }
                _chartCache.requests = null;
            }
            return;
        }
        if (typeof Chart === 'undefined') return;

        var el = document.getElementById('requestsChartData');
        var raw = el ? (el.textContent || '').trim() : '';
        var payload = _safeJsonParse(raw);
        if (!payload) return;

        var hash = _hashString(raw);
        if (_chartCache.requests && _chartCache.requests.hash === hash) return;

        // Destroy old instance if hash changed
        if (_chartCache.requests && _chartCache.requests.instance) {
            _chartCache.requests.instance.destroy();
        }

        var labels = payload.labels || [];
        var data = payload.data || [];

        var instance = new Chart(canvas, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Requests',
                    data: data,
                    borderColor: 'rgb(13, 148, 136)',
                    backgroundColor: 'rgba(13, 148, 136, 0.1)',
                    fill: true,
                    tension: 0.3,
                    pointRadius: 3,
                    pointHoverRadius: 5
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false }
                },
                scales: {
                    x: {
                        grid: { display: false },
                        ticks: { font: { size: 11 }, color: document.documentElement.classList.contains('dark') ? '#64748b' : '#9ca3af', maxTicksLimit: 12 }
                    },
                    y: {
                        beginAtZero: true,
                        grid: { color: document.documentElement.classList.contains('dark') ? 'rgba(51, 65, 85, 0.5)' : '#f3f4f6' },
                        ticks: {
                            font: { size: 11 },
                            color: document.documentElement.classList.contains('dark') ? '#64748b' : '#9ca3af',
                            stepSize: 1,
                            precision: 0
                        }
                    }
                }
            }
        });
        _chartCache.requests = { instance: instance, hash: hash };
    }

    function initSuspicionChart() {
        var canvas = document.getElementById('suspicionChart');
        if (!canvas) {
            if (_chartCache.suspicion) {
                if (_chartCache.suspicion.instance && typeof _chartCache.suspicion.instance.destroy === 'function') {
                    _chartCache.suspicion.instance.destroy();
                }
                _chartCache.suspicion = null;
            }
            return;
        }
        if (typeof Chart === 'undefined') return;

        var el = document.getElementById('suspicionChartData');
        var raw = el ? (el.textContent || '').trim() : '';
        var payload = _safeJsonParse(raw);
        if (!payload) return;

        var labels = payload.labels || [];
        var data = payload.data || [];
        var total = 0;
        for (var i = 0; i < data.length; i++) {
            total += Number(data[i] || 0);
        }
        if (total <= 0) return;

        var hash = _hashString(raw);
        if (_chartCache.suspicion && _chartCache.suspicion.hash === hash) return;

        if (_chartCache.suspicion && _chartCache.suspicion.instance) {
            _chartCache.suspicion.instance.destroy();
        }

        var instance = new Chart(canvas, {
            type: 'doughnut',
            data: {
                labels: labels,
                datasets: [{
                    data: data,
                    backgroundColor: [
                        'rgb(52, 211, 153)',
                        'rgb(251, 191, 36)',
                        'rgb(249, 115, 22)',
                        'rgb(239, 68, 68)'
                    ],
                    borderWidth: 0,
                    hoverOffset: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                cutout: '60%',
                plugins: {
                    legend: { display: false }
                }
            }
        });
        _chartCache.suspicion = { instance: instance, hash: hash };
    }

    // ──────────────────────────────────────────────
    // Difficulty picker (controls page)
    // ──────────────────────────────────────────────

    function _setDifficultyActive(picker, hidden, level) {
        if (!picker || !hidden) return;
        if (!level) return;
        hidden.value = level;
        var buttons = picker.querySelectorAll('.difficulty-option');
        for (var i = 0; i < buttons.length; i++) {
            var btn = buttons[i];
            var isActive = btn.dataset.level === level;
            btn.classList.toggle('is-active', isActive);
            btn.setAttribute('aria-checked', isActive ? 'true' : 'false');
        }
    }

    function _initSinglePicker(picker) {
        var hiddenId = picker.dataset.hidden || 'difficulty-active-input';
        var hidden = document.getElementById(hiddenId);
        if (!hidden) return;
        var first = picker.querySelector('.difficulty-option');
        var initial = hidden.value || (first ? first.dataset.level : '');
        _setDifficultyActive(picker, hidden, initial);
    }

    function initDifficultyPickerState() {
        var pickers = document.querySelectorAll('.difficulty-picker');
        for (var i = 0; i < pickers.length; i++) {
            _initSinglePicker(pickers[i]);
        }
    }

    // ──────────────────────────────────────────────
    // Confirm modal (controls + prompts pages)
    // ──────────────────────────────────────────────

    var _confirmForm = null;

    function _showConfirmModal(form, title, message, severity) {
        _confirmForm = form;
        var btn = document.getElementById('confirm-action-btn');
        if (!btn) return;

        document.getElementById('confirm-title').textContent = title;
        document.getElementById('confirm-message').innerHTML = message;

        document.getElementById('confirm-icon-warning').style.display = 'none';
        document.getElementById('confirm-icon-danger').style.display = 'none';
        if (severity === 'danger') {
            document.getElementById('confirm-icon-danger').style.display = 'flex';
            btn.className = 'flex-1 px-4 py-3 text-sm font-semibold rounded-br-2xl transition-colors';
            btn.style.background = 'var(--ds-danger)';
            btn.style.color = '#fff';
        } else {
            document.getElementById('confirm-icon-warning').style.display = 'flex';
            btn.className = 'flex-1 px-4 py-3 text-sm font-semibold rounded-br-2xl transition-colors';
            btn.style.background = 'var(--ds-warning)';
            btn.style.color = '#fff';
        }

        _showModal('confirm-modal', 'confirm-modal-box');
    }

    function _hideConfirmModal() {
        _hideModal('confirm-modal', 'confirm-modal-box');
        _confirmForm = null;
    }

    // ──────────────────────────────────────────────
    // Log row chat toggle (logs page)
    // ──────────────────────────────────────────────

    function _toggleLogChat(rowEl, logId) {
        var inner = document.getElementById('chat-inner-' + logId);
        if (!inner) return;

        var isOpen = inner.classList.contains('open');

        // Close any other open detail (accordion)
        document.querySelectorAll('.chat-detail-inner.open').forEach(function (el) {
            el.classList.remove('open');
            var id = el.id.replace('chat-inner-', 'log-row-');
            var row = document.getElementById(id);
            if (row) row.classList.remove('chat-open');
        });

        if (isOpen) return;

        // Open this detail
        inner.classList.add('open');
        rowEl.classList.add('chat-open');

        // Lazy-load content via HTMX if not already loaded
        var contentDiv = document.getElementById('chat-content-' + logId);
        if (contentDiv && !contentDiv.dataset.loaded) {
            htmx.ajax('GET', '/panel/logs/' + logId + '/chat', {
                target: '#chat-content-' + logId,
                swap: 'innerHTML'
            });
            contentDiv.dataset.loaded = 'true';
        }
    }

    // ──────────────────────────────────────────────
    // User detail log/chat view toggle
    // ──────────────────────────────────────────────

    var _chatViewLoaded = false;

    function _showLogView() {
        var logView = document.getElementById('log-view-content');
        var chatView = document.getElementById('chat-view-content');
        var title = document.getElementById('interactions-title');
        var btnLog = document.getElementById('btn-log-view');
        var btnChat = document.getElementById('btn-chat-view');
        if (!logView || !chatView) return;
        logView.style.display = '';
        chatView.style.display = 'none';
        if (title) title.textContent = 'Conversation Log (last 50)';
        if (btnLog) { btnLog.className = 'ds-btn-secondary ds-btn-sm'; btnLog.style.background = 'var(--ds-accent-soft)'; btnLog.style.color = 'var(--ds-accent)'; btnLog.style.borderColor = 'var(--ds-accent)'; }
        if (btnChat) { btnChat.className = 'ds-btn-secondary ds-btn-sm'; btnChat.style.background = ''; btnChat.style.color = ''; btnChat.style.borderColor = ''; }
    }

    function _showChatView() {
        var logView = document.getElementById('log-view-content');
        var chatView = document.getElementById('chat-view-content');
        var title = document.getElementById('interactions-title');
        var btnLog = document.getElementById('btn-log-view');
        var btnChat = document.getElementById('btn-chat-view');
        if (!logView || !chatView) return;
        logView.style.display = 'none';
        chatView.style.display = '';
        if (title) title.textContent = 'All Interactions (Chat View)';
        if (btnChat) { btnChat.className = 'ds-btn-secondary ds-btn-sm'; btnChat.style.background = 'var(--ds-accent-soft)'; btnChat.style.color = 'var(--ds-accent)'; btnChat.style.borderColor = 'var(--ds-accent)'; }
        if (btnLog) { btnLog.className = 'ds-btn-secondary ds-btn-sm'; btnLog.style.background = ''; btnLog.style.color = ''; btnLog.style.borderColor = ''; }
        if (!_chatViewLoaded && btnChat && btnChat.dataset.chatUrl) {
            htmx.ajax('GET', btnChat.dataset.chatUrl, {
                target: '#chat-view-inner',
                swap: 'innerHTML'
            });
            _chatViewLoaded = true;
        }
    }

    // ──────────────────────────────────────────────
    // Sidebar toggle (base.html)
    // ──────────────────────────────────────────────

    function _toggleSidebar() {
        var sidebar = document.getElementById('sidebar');
        var overlay = document.getElementById('sidebar-overlay');
        if (!sidebar) return;
        sidebar.classList.toggle('-translate-x-full');
        if (overlay) overlay.classList.toggle('hidden');
    }

    function _closeSidebar() {
        var sidebar = document.getElementById('sidebar');
        var overlay = document.getElementById('sidebar-overlay');
        if (sidebar) sidebar.classList.add('-translate-x-full');
        if (overlay) overlay.classList.add('hidden');
    }

    // ──────────────────────────────────────────────
    // Dark mode toggle (base.html)
    // ──────────────────────────────────────────────

    function _toggleDarkMode() {
        var next = !document.documentElement.classList.contains('dark');
        document.documentElement.classList.toggle('dark', next);
        _updateDarkIcons(next);
        try { localStorage.setItem('darkMode', String(next)); } catch (_e) {}
    }

    function _updateDarkIcons(isDark) {
        var sun = document.getElementById('icon-sun');
        var moon = document.getElementById('icon-moon');
        if (!sun || !moon) return;
        sun.classList.toggle('hidden', !isDark);
        moon.classList.toggle('hidden', isDark);
    }

    function _applyStoredDarkMode() {
        var isDark = false;
        try { isDark = localStorage.getItem('darkMode') === 'true'; } catch (_e) {}
        document.documentElement.classList.toggle('dark', isDark);
        _updateDarkIcons(isDark);
    }

    // ──────────────────────────────────────────────
    // CSRF injection (base.html)
    // ──────────────────────────────────────────────

    function _readCookie(name) {
        var prefix = name + '=';
        var parts = document.cookie ? document.cookie.split('; ') : [];
        for (var i = 0; i < parts.length; i++) {
            if (parts[i].indexOf(prefix) === 0) {
                return decodeURIComponent(parts[i].slice(prefix.length));
            }
        }
        return '';
    }

    function _injectCsrfHiddenFields() {
        var token = _readCookie('panel_csrf');
        if (!token) return;
        var forms = document.querySelectorAll('form');
        forms.forEach(function (form) {
            var existing = form.querySelector('input[name="csrf_token"]');
            if (!existing) {
                var input = document.createElement('input');
                input.type = 'hidden';
                input.name = 'csrf_token';
                input.value = token;
                form.appendChild(input);
            } else {
                existing.value = token;
            }
        });
    }

    // ──────────────────────────────────────────────
    // Toast notifications (Step 3)
    // ──────────────────────────────────────────────

    var _toastId = 0;

    function showToast(message, type, duration) {
        type = type || 'info';
        duration = duration || 5000;
        var container = document.getElementById('toast-container');
        if (!container) return;

        var id = 'toast-' + (++_toastId);
        var icons = {
            success: '<path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"/>',
            error: '<path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m9-.75a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 3.75h.008v.008H12v-.008Z"/>',
            warning: '<path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"/>',
            info: '<path stroke-linecap="round" stroke-linejoin="round" d="m11.25 11.25.041-.02a.75.75 0 0 1 1.063.852l-.708 2.836a.75.75 0 0 0 1.063.853l.041-.021M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9-3.75h.008v.008H12V8.25Z"/>'
        };

        var toast = document.createElement('div');
        toast.id = id;
        toast.className = 'toast toast-' + type;
        toast.setAttribute('role', 'alert');
        toast.innerHTML =
            '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="toast-icon">' +
            (icons[type] || icons.info) +
            '</svg>' +
            '<span class="toast-text">' + _escapeHtml(message) + '</span>' +
            '<button type="button" class="toast-close" data-dismiss-toast="' + id + '" aria-label="Dismiss">' +
            '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-4 h-4">' +
            '<path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12"/>' +
            '</svg></button>';

        container.appendChild(toast);
        // Trigger slide-in
        requestAnimationFrame(function () {
            toast.classList.add('toast-visible');
        });

        // Auto-dismiss
        if (type !== 'error') {
            setTimeout(function () { dismissToast(id); }, duration);
        }
    }

    function dismissToast(id) {
        var toast = document.getElementById(id);
        if (!toast) return;
        toast.classList.add('toast-exit');
        setTimeout(function () {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 300);
    }

    // ──────────────────────────────────────────────
    // Flash message auto-dismiss (Step 4)
    // ──────────────────────────────────────────────

    function initFlashDismiss() {
        var flashes = document.querySelectorAll('[data-auto-dismiss]');
        flashes.forEach(function (el) {
            if (el._autoDismissInit) return;
            el._autoDismissInit = true;
            var delay = parseInt(el.dataset.autoDismiss, 10) || 5000;
            setTimeout(function () {
                el.classList.add('flash-exit');
                setTimeout(function () {
                    if (el.parentNode) el.parentNode.removeChild(el);
                }, 400);
            }, delay);
        });
    }

    // ──────────────────────────────────────────────
    // Client-side form validation (Step 7)
    // ──────────────────────────────────────────────

    function _validateForm(form) {
        // Clear previous errors
        form.querySelectorAll('.field-error').forEach(function (el) {
            if (el.parentNode) el.parentNode.removeChild(el);
        });
        form.querySelectorAll('.field-invalid').forEach(function (el) {
            el.classList.remove('field-invalid');
        });

        var inputs = form.querySelectorAll('input, textarea, select');
        for (var i = 0; i < inputs.length; i++) {
            var input = inputs[i];
            var value = (input.value || '').trim();
            var error = null;

            // required
            if (input.hasAttribute('required') && !value) {
                error = 'This field is required.';
            }

            // minlength
            if (!error && input.hasAttribute('minlength')) {
                var min = parseInt(input.getAttribute('minlength'), 10);
                if (value.length > 0 && value.length < min) {
                    error = 'Must be at least ' + min + ' characters.';
                }
            }

            // pattern
            if (!error && input.hasAttribute('pattern') && value) {
                var regex = new RegExp('^(?:' + input.getAttribute('pattern') + ')$');
                if (!regex.test(value)) {
                    error = 'Invalid format.';
                }
            }

            // data-validate-complexity
            if (!error && input.hasAttribute('data-validate-complexity') && value) {
                if (!/[a-z]/.test(value)) {
                    error = 'Must contain a lowercase letter.';
                } else if (!/[A-Z]/.test(value)) {
                    error = 'Must contain an uppercase letter.';
                } else if (!/[0-9]/.test(value)) {
                    error = 'Must contain a digit.';
                }
            }

            // data-validate-match
            if (!error && input.hasAttribute('data-validate-match') && value) {
                var matchName = input.getAttribute('data-validate-match');
                var matchInput = form.querySelector('[name="' + matchName + '"]');
                if (matchInput && matchInput.value !== input.value) {
                    error = 'Does not match.';
                }
            }

            if (error) {
                input.classList.add('field-invalid');
                var errDiv = document.createElement('div');
                errDiv.className = 'field-error';
                errDiv.textContent = error;
                input.parentNode.insertBefore(errDiv, input.nextSibling);
                input.focus();
                return false;
            }
        }
        return true;
    }

    // Capture-phase submit handler — fires before HTMX
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (!form || !form.hasAttribute('data-validate')) return;
        if (!_validateForm(form)) {
            e.preventDefault();
            e.stopPropagation();
        }
    }, true);

    // ──────────────────────────────────────────────
    // Event delegation (single click handler)
    // ──────────────────────────────────────────────

    document.addEventListener('click', function (e) {
        var target = e.target;
        if (!target || !target.closest) return;

        // Sidebar toggle
        if (target.closest('#sidebar-toggle')) {
            _toggleSidebar();
            return;
        }
        // Sidebar overlay close
        if (target.closest('#sidebar-overlay')) {
            _closeSidebar();
            return;
        }
        // Dark mode toggle
        if (target.closest('#dark-toggle')) {
            _toggleDarkMode();
            return;
        }

        // Flash dismiss close button
        var flashDismiss = target.closest('.flash-dismiss');
        if (flashDismiss) {
            var flash = flashDismiss.closest('.flash-message');
            if (flash) {
                flash.classList.add('flash-exit');
                setTimeout(function () {
                    if (flash.parentNode) flash.parentNode.removeChild(flash);
                }, 400);
            }
            return;
        }

        // Toast dismiss close button
        var toastDismiss = target.closest('[data-dismiss-toast]');
        if (toastDismiss) {
            dismissToast(toastDismiss.dataset.dismissToast);
            return;
        }

        // Simple confirm dialogs: data-confirm="Are you sure?"
        var confirmBtn = target.closest('[data-confirm]');
        if (confirmBtn) {
            if (!confirm(confirmBtn.dataset.confirm)) {
                e.preventDefault();
            }
            return;
        }

        // Confirm modal trigger: .js-confirm-btn with data-confirm-title/message/severity
        var modalBtn = target.closest('.js-confirm-btn');
        if (modalBtn) {
            _showConfirmModal(
                modalBtn.closest('form'),
                modalBtn.dataset.confirmTitle,
                modalBtn.dataset.confirmMessage,
                modalBtn.dataset.confirmSeverity
            );
            return;
        }
        // Confirm modal actions
        if (target.closest('#confirm-action-btn')) {
            if (_confirmForm) _confirmForm.submit();
            _hideConfirmModal();
            return;
        }
        if (target.closest('#confirm-cancel-btn') || target.id === 'confirm-backdrop') {
            _hideConfirmModal();
            return;
        }

        // Difficulty picker (any .difficulty-picker)
        var diffBtn = target.closest('.difficulty-option');
        if (diffBtn) {
            var picker = diffBtn.closest('.difficulty-picker');
            if (picker) {
                var hiddenId = picker.dataset.hidden || 'difficulty-active-input';
                var hidden = document.getElementById(hiddenId);
                if (hidden) _setDifficultyActive(picker, hidden, diffBtn.dataset.level);
            }
            return;
        }

        // Log row chat toggle
        var logRow = target.closest('[data-log-toggle]');
        if (logRow) {
            _toggleLogChat(logRow, logRow.dataset.logToggle);
            return;
        }

        // Toggle next sibling table row (e.g. inline change-password form)
        var toggleNextRow = target.closest('[data-toggle-next-row]');
        if (toggleNextRow) {
            var row = toggleNextRow.closest('tr');
            if (row && row.nextElementSibling) {
                row.nextElementSibling.classList.toggle('hidden');
            }
            return;
        }

        // Prompt editor tab switching
        var promptTab = target.closest('.prompt-tab');
        if (promptTab) {
            var tabs = document.querySelectorAll('.prompt-tab');
            tabs.forEach(function (t) {
                if (t.dataset.tab === promptTab.dataset.tab) {
                    t.classList.add('border-b-2');
                    t.classList.remove('ds-text-secondary');
                    t.style.borderColor = 'var(--ds-accent)';
                    t.style.color = 'var(--ds-accent)';
                    t.setAttribute('aria-selected', 'true');
                } else {
                    t.classList.remove('border-b-2');
                    t.classList.add('ds-text-secondary');
                    t.style.borderColor = '';
                    t.style.color = '';
                    t.setAttribute('aria-selected', 'false');
                }
            });
            document.querySelectorAll('.prompt-panel').forEach(function (p) {
                p.classList.toggle('hidden', p.dataset.panel !== promptTab.dataset.tab);
            });
            return;
        }

        // Edit flag modal trigger
        var editFlagBtn = target.closest('[data-edit-flag]');
        if (editFlagBtn) {
            var flagId = editFlagBtn.dataset.flagId;
            var flagValue = editFlagBtn.dataset.flagValue;
            var flagMessage = editFlagBtn.dataset.flagMessage;
            var flagTag = editFlagBtn.dataset.flagTag || '';
            var flagNotify = editFlagBtn.dataset.flagNotify || '';
            var flagNotifyMessage = editFlagBtn.dataset.flagNotifyMessage || '';
            var form = document.getElementById('edit-flag-form');
            if (form) {
                form.action = '/panel/flags/' + flagId + '/update';
                document.getElementById('edit-flag-value').value = flagValue;
                document.getElementById('edit-flag-message').value = flagMessage;
                var tagInput = document.getElementById('edit-flag-tag');
                if (tagInput) tagInput.value = flagTag;
                var notifyInput = document.getElementById('edit-flag-notify');
                if (notifyInput) notifyInput.checked = flagNotify === 'on';
                var notifyMsgInput = document.getElementById('edit-flag-notify-message');
                if (notifyMsgInput) notifyMsgInput.value = flagNotifyMessage;
            }
            _showModal('edit-flag-modal', 'edit-flag-box');
            return;
        }
        // Edit flag modal cancel / backdrop close
        if (target.closest('#edit-flag-cancel') || target.id === 'edit-flag-backdrop') {
            _hideModal('edit-flag-modal', 'edit-flag-box');
            return;
        }

        // User detail: log/chat view toggle
        if (target.closest('#btn-log-view')) {
            _showLogView();
            return;
        }
        if (target.closest('#btn-chat-view')) {
            _showChatView();
            return;
        }
    });

    // ──────────────────────────────────────────────
    // Flags drag-and-drop reorder (SortableJS)
    // ──────────────────────────────────────────────

    function initFlagsSortable() {
        var tbody = document.getElementById('flags-list');
        if (!tbody || tbody._sortableInit) return;
        if (typeof Sortable === 'undefined') return;
        tbody._sortableInit = true;
        new Sortable(tbody, {
            handle: '.flag-drag-handle',
            animation: 150,
            ghostClass: 'sortable-ghost',
            onEnd: function () {
                var rows = tbody.querySelectorAll('[data-flag-id]');
                var ids = Array.from(rows).map(function (r) { return parseInt(r.dataset.flagId); });
                // Update position numbers in the UI
                rows.forEach(function (r, i) {
                    var posEl = r.querySelector('.flag-position');
                    if (posEl) posEl.textContent = '#' + (i + 1);
                });
                // Send to server
                fetch('/panel/flags/reorder', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': _readCookie('panel_csrf')
                    },
                    body: JSON.stringify({ ids: ids })
                });
            }
        });
    }

    // ──────────────────────────────────────────────
    // Keyboard handlers
    // ──────────────────────────────────────────────

    // Escape key closes modals
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            _hideConfirmModal();
            _hideModal('edit-flag-modal', 'edit-flag-box');
        }
    });

    // Difficulty picker keyboard nav (any .difficulty-picker)
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
        var btn = e.target && e.target.closest ? e.target.closest('.difficulty-option') : null;
        if (!btn) return;
        var picker = btn.closest ? btn.closest('.difficulty-picker') : null;
        if (!picker) return;

        var buttons = Array.prototype.slice.call(picker.querySelectorAll('.difficulty-option'));
        var idx = buttons.indexOf(btn);
        if (idx < 0) return;

        e.preventDefault();
        var nextIdx = e.key === 'ArrowRight'
            ? Math.min(buttons.length - 1, idx + 1)
            : Math.max(0, idx - 1);
        var next = buttons[nextIdx];
        if (next && typeof next.focus === 'function') {
            next.focus();
        }
        var hiddenId = picker.dataset.hidden || 'difficulty-active-input';
        var hidden = document.getElementById(hiddenId);
        if (hidden) {
            _setDifficultyActive(picker, hidden, next.dataset.level);
        }
    });

    // ──────────────────────────────────────────────
    // HTMX event handlers
    // ──────────────────────────────────────────────

    // Suppress auto-refresh polling when a chat detail is expanded (logs page)
    document.body.addEventListener('htmx:beforeSwap', function (evt) {
        if (evt.detail.target && evt.detail.target.id === 'log-table') {
            if (document.querySelector('.chat-detail-inner.open')) {
                evt.detail.shouldSwap = false;
            }
        }
    });

    // CSRF header injection for HTMX requests
    document.body.addEventListener('htmx:configRequest', function (e) {
        var token = _readCookie('panel_csrf');
        if (token) {
            e.detail.headers['X-CSRF-Token'] = token;
        }
    });

    // HTMX error handling → toast notifications (Step 3)
    document.body.addEventListener('htmx:sendError', function () {
        showToast('Network error. Please check your connection.', 'error');
    });

    document.body.addEventListener('htmx:responseError', function (e) {
        var status = e.detail.xhr ? e.detail.xhr.status : 0;
        var messages = {
            403: 'Forbidden. Your session may have expired.',
            404: 'Page not found.',
            500: 'Server error. Please try again later.'
        };
        var msg = messages[status] || ('Request failed (HTTP ' + status + ').');
        showToast(msg, 'error');
    });

    // ──────────────────────────────────────────────
    // Init on load + HTMX swaps
    // ──────────────────────────────────────────────

    function initAfterSwap() {
        _setChartDefaults();
        initRequestsChart();
        initSuspicionChart();
        initDifficultyPickerState();
        initFlagsSortable();
        _applyStoredDarkMode();
        _injectCsrfHiddenFields();
        initFlashDismiss();
        // Reset chat view loaded flag on page navigation
        _chatViewLoaded = false;
    }

    document.addEventListener('DOMContentLoaded', initAfterSwap);
    document.addEventListener('htmx:afterSettle', initAfterSwap);
})();
