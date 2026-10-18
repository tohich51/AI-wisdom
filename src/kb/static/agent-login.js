(() => {
  'use strict';

  const MAX_POLL_SECONDS = 120;
  const REQUEST_TIMEOUT_MS = 10000;
  const status = document.getElementById('agent-status');
  const challenge = document.getElementById('agent-approval');
  const requestId = document.getElementById('agent-request-id');
  const requestExpiry = document.getElementById('agent-request-expiry');
  const cancelButton = document.getElementById('agent-cancel');
  let started = false;
  let stopped = false;
  let pairingId = null;
  let activeController = null;

  const delay = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

  function safeReturnTarget(value) {
    return typeof value === 'string'
      && value.length > 0
      && value.startsWith('/')
      && !value.startsWith('//')
      && !value.includes('\\');
  }

  function setStatus(message, isError = false) {
    status.textContent = message;
    status.classList.toggle('error', isError);
  }

  function clearChallenge() {
    pairingId = null;
    challenge.hidden = true;
    challenge.removeAttribute('data-pairing-id');
    challenge.removeAttribute('data-nonce');
    challenge.removeAttribute('data-origin');
    requestId.textContent = '';
    requestExpiry.textContent = '';
    requestExpiry.removeAttribute('datetime');
  }

  function fail(message) {
    stopped = true;
    clearChallenge();
    cancelButton.disabled = true;
    setStatus(message, true);
  }

  async function postJSON(path, body) {
    const controller = new AbortController();
    activeController = controller;
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(path, {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(body),
        signal: controller.signal
      });
      let payload = null;
      try {
        payload = await response.json();
      } catch (_) {
        payload = null;
      }
      return { response, payload };
    } finally {
      window.clearTimeout(timeout);
      if (activeController === controller) activeController = null;
    }
  }

  async function cancelPairing() {
    if (stopped || !pairingId) return;
    const id = pairingId;
    stopped = true;
    if (activeController) activeController.abort();
    cancelButton.disabled = true;
    try {
      const result = await postJSON('/auth/agent/cancel', { pairing_id: id });
      clearChallenge();
      if (!result.response.ok) throw new Error('cancel_failed');
      setStatus('Вход отменён.');
    } catch (_) {
      clearChallenge();
      setStatus('Не удалось отменить вход.', true);
    }
  }

  async function start() {
    if (started) return;
    started = true;
    cancelButton.addEventListener('click', cancelPairing);

    const requestedReturn = new URLSearchParams(window.location.search).get('return_to');
    if (
      requestedReturn !== null
      && requestedReturn.trim() !== ''
      && !safeReturnTarget(requestedReturn)
    ) {
      fail('Недопустимый адрес возврата.');
      return;
    }

    try {
      const begun = await postJSON('/auth/agent/begin', {
        return_to: requestedReturn
      });
      if (!begun.response.ok) {
        fail(
          begun.response.status === 409
            ? 'В этой браузерной сессии уже выполнен вход.'
            : 'Не удалось начать вход.'
        );
        return;
      }
      const payload = begun.payload;
      if (
        !payload
        || typeof payload.pairing_id !== 'string'
        || typeof payload.nonce !== 'string'
        || payload.nonce.length < 16
        || payload.nonce.length > 256
        || typeof payload.expires_at !== 'string'
      ) {
        fail('Ответ входа повреждён.');
        return;
      }
      const expiresMs = Date.parse(payload.expires_at);
      if (!Number.isFinite(expiresMs) || expiresMs <= Date.now()) {
        fail('Срок входа истёк.');
        return;
      }

      pairingId = payload.pairing_id;
      challenge.dataset.pairingId = payload.pairing_id;
      challenge.dataset.nonce = payload.nonce;
      challenge.dataset.origin = window.location.origin;
      requestId.textContent = payload.pairing_id;
      requestExpiry.dateTime = payload.expires_at;
      requestExpiry.textContent = new Date(expiresMs).toLocaleString();
      challenge.hidden = false;
      cancelButton.disabled = false;
      setStatus('Ожидается подтверждение входа…');

      const deadline = Math.min(Date.now() + MAX_POLL_SECONDS * 1000, expiresMs);
      for (let attempt = 0; attempt < MAX_POLL_SECONDS; attempt += 1) {
        await delay(1000);
        if (stopped) return;
        if (Date.now() >= deadline) break;

        const result = await postJSON('/auth/agent/complete', {
          pairing_id: pairingId,
          nonce: payload.nonce
        });
        if (stopped) return;
        if (result.response.status === 202) {
          if (!result.payload || result.payload.status !== 'pending') {
            fail('Ответ ожидания повреждён.');
            return;
          }
          continue;
        }
        if (!result.response.ok) {
          fail('Вход не подтверждён.');
          return;
        }
        if (!result.payload || result.payload.authenticated !== true) {
          fail('Ответ входа повреждён.');
          return;
        }
        if (
          result.payload.safe_return_to !== null
          && typeof result.payload.safe_return_to !== 'string'
        ) {
          fail('Недопустимый адрес возврата.');
          return;
        }
        const destination = result.payload.safe_return_to || '/app/libraries';
        if (!safeReturnTarget(destination)) {
          fail('Недопустимый адрес возврата.');
          return;
        }
        stopped = true;
        cancelButton.disabled = true;
        setStatus('Вход подтверждён. Переходим…');
        window.location.replace(destination);
        return;
      }
      fail('Время ожидания подтверждения истекло.');
    } catch (_) {
      if (!stopped) fail('Не удалось связаться с сервером.');
    }
  }

  window.addEventListener('load', start, { once: true });
})();
