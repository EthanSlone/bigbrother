// Two-factor setup, shared by sign-in (when it's required) and Your account.
import {$, api, esc, toast} from './app.js';

const onPhone = () => matchMedia('(pointer: coarse)').matches;

// Renders the steps into `el`; calls onDone() after the recovery codes have been saved.
// `password` is needed when moving an existing setup to a new phone.
export async function enrol(el, {password, onDone, heading = 'Set up two-factor sign-in'} = {}) {
  const b = await api('/api/me/2fa/begin', {method: 'POST', body: password ? {password} : {}});
  const key = esc(b.secret.replace(/(.{4})/g, '$1 ').trim());
  // On a phone you can't scan your own screen: open the authenticator app directly instead.
  const phone = onPhone();
  el.innerHTML = `<h3 class="tf-h">${esc(heading)}</h3>
    <div class="enrol${phone ? ' phone' : ''}">
      ${phone ? '' : `<div class="qr">${b.qr}</div>`}
      <div class="form" style="gap:12px">
        ${phone
          ? `<p>1. Add BigBrother to your authenticator app (Google Authenticator works).</p>
             <div><a class="btn primary" href="${esc(b.uri)}">Add to authenticator app</a></div>
             <p class="hint muted">Nothing happens? Add an account by hand in the app and type this key:</p>`
          : `<p>1. In your authenticator app (Google or Microsoft Authenticator, 1Password, Authy…), add an account and scan this code. Or type the key:</p>`}
        <code class="key">${key}</code>
        <form class="form" id="tf-confirm" style="gap:12px"><label class="field">2. Enter the 6-digit code it shows
          <input type="text" name="code" class="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required></label>
          <p class="error" role="alert" hidden></p><div><button class="btn primary" type="submit">Turn on</button></div></form>
      </div></div>`;
  const cf = $('#tf-confirm', el), err = $('.error', cf);
  if (!phone) $('input', cf).focus();
  $('input', cf).addEventListener('input', e => { if (/^\d{6}$/.test(e.target.value)) cf.requestSubmit(); });
  cf.onsubmit = async e => {
    e.preventDefault(); err.hidden = true;
    try {
      const r = await api('/api/me/2fa/confirm', {method: 'POST', body: {code: cf.code.value}});
      el.innerHTML = `<h3 class="tf-h">Two-factor sign-in is on</h3>
        <p>Save these recovery codes somewhere safe, like your password manager. Each one gets you in once if you lose your phone.
          They won't be shown again.</p>
        <div class="recovery">${r.recovery_codes.map(c => `<code>${esc(c)}</code>`).join('')}</div>
        <div style="display:flex;gap:10px;flex-wrap:wrap"><button class="btn" id="tf-copy" type="button">Copy codes</button>
          <button class="btn primary" id="tf-done" type="button">I've saved them</button></div>`;
      $('#tf-copy', el).onclick = () => navigator.clipboard.writeText(r.recovery_codes.join('\n'))
        .then(() => toast('Copied'), () => toast("Couldn't copy: select the codes and copy them instead"));
      $('#tf-done', el).onclick = () => onDone && onDone();
    } catch (ex) { err.textContent = ex.message; err.hidden = false; cf.code.value = ''; }
  };
}
