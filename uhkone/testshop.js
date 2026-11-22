'use strict';

// Reviewed public DOM skeletons, local state and fictional writes only.
const prefix = '/webshop/reservations2/';
const modal = document.querySelector('#modal');
let templates, rows = [], cartRows = [], reservations = [], user;
let currentView = null, refreshing = false, readError = false, cartLoaded = false;
let snapshotVersion = 0, cartVersion = 0;
const money = cents => new Intl.NumberFormat('fi-FI', {style:'currency', currency:'EUR'}).format(Number(cents) / 100);
const message = text => { readError = false; document.querySelector('#message').textContent = text; };
const showReadError = error => { message(error.message); readError = true; };

function plain(value) {
  const node = document.createElement('textarea');
  node.innerHTML = String(value ?? '').replace(/<br\s*\/?\s*>/gi, ' ').replace(/<[^>]*>/g, ' ');
  return node.value.replace(/\s+/g, ' ').trim();
}
function clone(id) {
  const template = templates?.getElementById(id);
  if (!template || template.tagName !== 'TEMPLATE') throw Error('Testin HTML-malli puuttuu: ' + id);
  return template.content.cloneNode(true);
}
function slot(root, name, value) {
  const node = root.querySelector('[data-slot="' + name + '"]');
  if (!node) throw Error('Testimallin tietopaikka puuttuu: ' + name);
  if (value !== undefined) node.textContent = value;
  return node;
}
async function api(path, content, rpc = false) {
  const options = {};
  if (content !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type':rpc ? 'application/x-www-form-urlencoded' : 'application/json'};
    options.body = rpc ? new URLSearchParams({input_format:'json', output_format:'json', content:JSON.stringify(content)}) : JSON.stringify(content);
  }
  const response = await fetch(path, options), data = await response.json();
  const errors = [data.errors, data.user_errors].flatMap(group => group || []);
  if (!response.ok || errors.length) {
    throw Error(errors.join(', ') || 'Testipalvelu vastasi ' + response.status);
  }
  return rpc ? data.result : data;
}
const rpc = (path, content = '') => api('/webshop/call/' + path, content, true);
const price = row => Number(row.bundle_price ?? row.price ?? row.fare_products[0].price);
const courseId = row => Number(row.reservation_event_group_id);
const courseUrl = row => prefix + 'reservations/9/' + row.location_id + '/' + row.reservation_group_id + '/-/' + courseId(row);
const selectedId = () => Number(location.pathname.match(/\/reservations\/\d+\/[\d-]+\/[\d-]+\/-\/(\d+)\/?$/)?.[1] || 0);
const participantName = id => Number(id) === Number(user.account_id) ? user.name : user.linked_accounts.find(account => Number(account.account_id) === Number(id))?.name || 'Tuntematon testiosallistuja';

function details(row) {
  const view = clone('template__eventgroup_details');
  const date = value => String(value || '').slice(0, 10).split('-').reverse().join('.');
  const weekdays = ['ma', 'ti', 'ke', 'to', 'pe', 'la', 'su'];
  slot(view, 'location', plain(row.location));
  slot(view, 'dates', date(row.start) + '–' + date(row.end));
  slot(view, 'schedule', (row.weekdays || []).map(day => (day.weekdays || []).map(index => weekdays[index]).join(', ') + ' ' + plain(day.times)).join(' · '));
  return view;
}
function availability(row) {
  if (!row.is_reservable) return 'Ilmoittautuminen alkaa ' + new Date(row.public_reservation_start).toLocaleString('fi-FI', {timeZone:'Europe/Helsinki'});
  if (Number(row.free) > 0) return row.free + ' / ' + row.capacity + ' paikkaa vapaana';
  return Number(row.free_queue) > 0 ? row.free_queue + ' jonopaikkaa vapaana' : 'Täynnä';
}
function renderReservations(row) {
  const view = clone('template__eventgroup_reservations');
  slot(view, 'status-heading', 'Tila'); slot(view, 'participant-heading', 'Osallistujan nimi');
  const body = view.querySelector('tbody'), prototype = body.querySelector('tr').cloneNode(true);
  body.replaceChildren();
  for (const reservation of reservations.filter(item => courseId(item) === courseId(row))) {
    const line = prototype.cloneNode(true), status = Number(reservation.reservation_status_id);
    line.className = status === 7 ? 'warning pending' : status === 1 || status === 3 ? 'warning' : 'success';
    slot(line, 'status', status === 7 ? 'Ei vahvistettu' : status === 1 || status === 3 ? 'Jonossa : ' + reservation.queue_number : 'Varattu');
    slot(line, 'participant', participantName(reservation.reservation_account_id));
    body.append(line);
  }
  return body.children.length ? view : document.createDocumentFragment();
}
function populateFilters() {
  for (const [selector, field] of [['#locationFilter','location'], ['#groupFilter','reservation_group_name']]) {
    const select = document.querySelector(selector), previous = select.value;
    const options = [...new Set(rows.map(row => plain(row[field])).filter(Boolean))].sort((a,b) => a.localeCompare(b, 'fi'));
    while (select.options.length > 1) select.remove(1);
    for (const value of options) select.add(new Option(value, value));
    select.value = previous;
  }
  document.querySelector('#courseFilters').hidden = Boolean(selectedId());
}
function renderCourses() {
  if (!templates || !user) return;
  const selected = selectedId(), locationFilter = document.querySelector('#locationFilter').value;
  const groupFilter = document.querySelector('#groupFilter').value;
  const search = document.querySelector('#courseSearch').value.toLocaleLowerCase('fi');
  const visible = rows.filter(row => selected ? courseId(row) === selected :
    (!locationFilter || plain(row.location) === locationFilter) && (!groupFilter || plain(row.reservation_group_name) === groupFilter) && plain(row.name).toLocaleLowerCase('fi').includes(search));
  const target = document.querySelector('#courses'); target.replaceChildren();
  for (const row of visible) {
    const view = clone('template_eventgroup'), card = view.querySelector('.eventgroup');
    card.dataset.courseId = courseId(row);
    card.querySelector('h4.title').textContent = plain(row.name);
    slot(card, 'details').replaceChildren(details(row));
    slot(card, 'reservations').replaceChildren(renderReservations(row));
    card.querySelector('.priceContainer .price').textContent = money(price(row));
    const label = slot(card, 'availability', availability(row));
    label.className = 'label ' + (!row.is_reservable ? 'label-important' : Number(row.free) > 0 ? 'label-success' : 'label-warning');
    const held = reservations.some(item => courseId(item) === courseId(row) && [2,4,5,7].includes(Number(item.reservation_status_id)));
    const queued = reservations.some(item => courseId(item) === courseId(row) && [1,3].includes(Number(item.reservation_status_id)));
    const buy = card.querySelector('button.buy'), queue = card.querySelector('button.queue');
    buy.textContent = 'Osta'; buy.hidden = !row.is_reservable || Number(row.free) <= 0 || held; buy.disabled = buy.hidden;
    buy.onclick = () => participants(row);
    queue.textContent = 'Jono'; queue.hidden = !row.is_reservable || Number(row.free) > 0 || Number(row.free_queue) <= 0 || held || queued; queue.disabled = queue.hidden;
    queue.onclick = () => participants(row, true);
    if (!selected) {
      const link = document.createElement('a'); link.className = 'course-link'; link.href = courseUrl(row); link.textContent = 'Avaa kurssi'; card.append(link);
    }
    target.append(view);
  }
  document.querySelector('#courseCount').textContent = visible.length + ' kurssia';
  if (!visible.length) target.textContent = 'Valituilla ehdoilla ei löytynyt testikursseja.';
}
function closeModal() {
  if (currentView?.busy) return;
  currentView = null; R2.app.modal.currentView = null; modal.hidden = true;
  document.querySelector('#modalBackdrop').hidden = true; document.body.classList.remove('modal-open');
}
function showModal(view, fragment) {
  currentView = view; R2.app.modal.currentView = view;
  modal.replaceChildren(fragment); modal.hidden = false;
  document.querySelector('#modalBackdrop').hidden = false; document.body.classList.add('modal-open');
  for (const button of modal.querySelectorAll('.close, .closeButton, .continueShopping')) {
    button.onclick = closeModal;
    if (button.classList.contains('close')) { button.textContent = '×'; button.setAttribute('aria-label', 'Sulje'); }
    if (button.classList.contains('closeButton')) button.textContent = 'Peruuta';
    if (button.classList.contains('continueShopping')) button.textContent = 'Jatka ostoksia';
  }
  for (const form of modal.querySelectorAll('form')) form.onsubmit = event => event.preventDefault();
}
function participants(row, queue = false) {
  const fragment = clone('template__eventgroup_buy'), selection = clone('template__participant_select');
  const view = {kind:'participants', row, queue, busy:false};
  fragment.querySelector('.modal-header h3').textContent = plain(row.name);
  fragment.querySelector('.modal-body .price').textContent = money(price(row));
  slot(fragment, 'availability', availability(row)); slot(fragment, 'details').replaceChildren(details(row));
  const adult = selection.querySelector('input[name="participant"]');
  adult.value = user.account_id; adult.dataset.name = user.name; adult.checked = true;
  slot(selection, 'adult-name', 'Varaa kurssi itselleni: ' + user.name);
  slot(selection, 'child-label', 'Varaa kurssi lapselle');
  slot(selection, 'extra-information-heading', 'Lisätiedot');
  slot(selection, 'participant-heading', 'Valitse osallistuja');
  const children = slot(selection, 'child-rows'), prototype = slot(selection, 'child-row').cloneNode(true);
  children.replaceChildren();
  for (const account of user.linked_accounts) {
    const child = prototype.cloneNode(true), input = child.querySelector('input[name="participant"]');
    input.value = account.account_id; input.dataset.name = account.name; input.checked = false;
    slot(child, 'child-name', account.name); children.append(child);
  }
  selection.querySelector('#linkedAccount').hidden = true;
  slot(fragment, 'participants').replaceChildren(selection);
  const ok = fragment.querySelector('.modal-footer .ok'); ok.textContent = queue ? 'Varaa paikka jonossa' : 'Siirrä ostoskoriin';
  showModal(view, fragment);
  modal.querySelector('#linked-accounts').onchange = () => {
    for (const input of modal.querySelectorAll('input[name="participant"]')) input.checked = false;
    modal.querySelector('#linkedAccount').hidden = false;
  };
  adult.onchange = () => { modal.querySelector('#linked-accounts').checked = false; modal.querySelector('#linkedAccount').hidden = true; };
  ok.onclick = async event => {
    event.preventDefault();
    if (view.busy || currentView !== view) return;
    const selected = modal.querySelector('input[name="participant"]:checked');
    if (!selected || !user.linked_accounts.some(account => String(account.account_id) === selected.value)) {
      modal.querySelector('.alerts').textContent = 'Valitse kuvitteellinen Testilapsi ennen varaamista.'; return;
    }
    view.busy = true; ok.setAttribute('aria-disabled', 'true');
    const account = {account_id:selected.value, name:selected.dataset.name};
    try {
      if (queue) {
        reservations = await api(prefix + 'apiusereventgroups', {account, reservation_event_group_id:courseId(row), reservation_status_id:1});
        snapshotVersion++; renderCourses(); view.busy = false;
        if (currentView === view) closeModal();
        message('SIMULOITU JONOPAIKKA TALLENNETTU. Tämä ei ole maksettu kurssipaikka.');
      } else {
        const added = await api(prefix + 'apicart', {account, reservation_event_group_id:courseId(row), fare_product_id:row.fare_products[0].fare_product_id,
          quantity:1, price:price(row), title:row.name, campaign_code:null});
        cartVersion++; cartRows = added; cartLoaded = true; renderWidget(); view.busy = false;
        message('Testipaikka on ostoskorissa. Maksu on vielä tekemättä.');
        // Production opens the cart from the successful add response. A failed
        // follow-up catalogue read must not hide this authoritative cart.
        if (currentView === view) cartModal();
      }
      refreshCourses().catch(showReadError);
    } catch (error) {
      view.busy = false;
      // A lost reply can follow a committed write. Never resubmit automatically.
      message(error.message + ' — tarkista palvelimen tila ennen uutta yritystä.');
      try { await refreshState(); } catch (_) { /* Preserve the original failure. */ }
      if (currentView === view) closeModal();
    }
  };
}
function renderWidget() {
  if (!templates) return;
  const view = clone('template_cartwidget'), widget = view.querySelector('#cart'); widget.hidden = cartRows.length === 0;
  const total = cartRows.reduce((sum, item) => sum + Number(item.price), 0);
  slot(widget, 'count', String(cartRows.length)).parentElement.setAttribute('aria-label', 'Ostoskori: Tuotteet ' + cartRows.length);
  slot(widget, 'total', money(total)).parentElement.setAttribute('aria-label', 'Hinta ' + money(total));
  widget.querySelector('.buy').textContent = 'Ostoskori'; widget.querySelector('.buy').onclick = () => cartModal();
  document.querySelector('#cartMount').replaceChildren(view);
}
async function fetchCart() {
  const version = ++cartVersion, latest = await api(prefix + 'apicart');
  if (version === cartVersion) { cartRows = latest; cartLoaded = true; renderWidget(); }
  return cartRows;
}
function renderCart(view) {
  if (currentView !== view) return;
  const fragment = clone('template_cart');
  fragment.querySelector('.modal-header h3').textContent = 'Ostoskori';
  const emptyMessage = slot(fragment, 'empty-message', 'Ostoskorissa ei ole tuotteita.');
  emptyMessage.hidden = cartRows.length > 0;
  for (const [name, text] of Object.entries({'product-heading':'Tuote','vat-heading':'ALV','price-heading':'Hinta','net-label':'Veroton hinta','vat-label':'ALV yhteensä','total-label':'Yhteensä','balance-label':'Käytä saldoa maksamiseen','terms-label':'Hyväksyn simuloidun testivarauksen ehdot'})) slot(fragment, name, text);
  const body = fragment.querySelector('#cartItems tbody'); body.replaceChildren();
  for (const item of cartRows) {
    const row = rows.find(row => courseId(row) === Number(item.reservation_params.reservation_event_group_id));
    const line = clone('template__cartitem');
    slot(line, 'title', row ? plain(row.name) : 'Tuntematon testikurssi'); slot(line, 'price', money(item.price));
    // The tax split is explicit test data; it never changes the charged total.
    slot(line, 'vat', money(Number(item.price) - Math.round(Number(item.price) / 1.135)));
    const remove = line.querySelector('.remove'); remove.textContent = 'Poista';
    remove.onclick = async () => {
      if (view.busy || currentView !== view) return;
      view.busy = true; renderCart(view);
      try {
        const locked = await rpc('cart/islocked');
        if (locked.isLocked !== false) throw Error('Testikori on lukittu. Poisto keskeytettiin.');
        await rpc('cart/deleteCartItem', [{fare_product_id:item.fare_product_id, item_id:item.item_id}]);
        await refreshState(); message('Testivaraus poistettu ja paikka vapautettu.');
      } catch (error) { message(error.message); }
      finally { view.busy = false; renderCart(view); }
    };
    body.append(line);
  }
  const total = cartRows.reduce((sum, item) => sum + Number(item.price), 0);
  const net = cartRows.reduce((sum, item) => sum + Math.round(Number(item.price) / 1.135), 0);
  slot(fragment, 'net', money(net)); slot(fragment, 'vat', money(total - net)); slot(fragment, 'total', money(total));
  const balanceSection = slot(fragment, 'balance-section');
  balanceSection.hidden = !cartRows.length || !Number.isFinite(view.balance) || view.balance <= 0;
  const balanceSummary = document.createElement('span'); balanceSummary.className = 'balance-summary';
  balanceSummary.textContent = 'Kuvitteellinen saldo ' + money(view.balance || 0); balanceSection.append(balanceSummary);
  const terms = fragment.querySelector('#usageTermsCheckbox'), balance = fragment.querySelector('#useBalanceCheckbox');
  terms.checked = false; terms.parentElement.classList.add('terms-label'); balance.checked = false;
  const methods = slot(fragment, 'payment-methods'), prototype = slot(methods, 'payment-button').cloneNode(true);
  methods.replaceChildren();
  for (const type of view.types || []) {
    if (type.is_pay_later || !cartRows.length) continue;
    const action = prototype.cloneNode(true); action.removeAttribute('data-slot');
    action.className = 'btn btn-tiny btn-primary ' + (type.is_balance ? 'buyWithBalance' : 'buyCart');
    action.setAttribute('payment-type-id', type.payment_type_id); action.disabled = Boolean(type.is_disabled) || view.busy;
    action.textContent = type.is_balance ? 'Maksa testisaldolla' : 'FastPay (testimaksu)'; action.hidden = Boolean(type.is_balance);
    action.onclick = async () => {
      if (view.busy || currentView !== view) return;
      if (!terms.checked || balance.checked !== Boolean(type.is_balance)) {
        modal.querySelector('.alerts').textContent = 'Hyväksy testiehdot ja valitse saldo vain saldomaksuun.'; return;
      }
      view.busy = true;
      for (const button of modal.querySelectorAll('button')) button.disabled = true;
      try {
        // The visible detail page may be a different course from the cart.
        // The canonical shop return is valid for every local cart and alias.
        const returnPath = prefix;
        await rpc('cart/externalpaymentstart', [{return_url:returnPath + '?doPayment=success', cancel_url:returnPath + '?doPayment=cancel',
          balance_amount:type.is_balance ? total : null, payment_type_id:String(type.payment_type_id), service_group_id:9}]);
        await refreshState(); view.busy = false; closeModal();
        message('SIMULOITU MAKSU VALMIS. Tarkista varaus ja kuitti testin tilasta.');
      } catch (error) {
        message(error.message + ' — maksun tila tarkistetaan palvelimelta.');
        try { await refreshState(); } catch (_) { /* Never retry payment. */ }
        view.busy = false; closeModal();
      }
    };
    methods.append(action);
  }
  balance.onchange = () => {
    for (const action of methods.querySelectorAll('button')) action.hidden = balance.checked ? !action.classList.contains('buyWithBalance') : action.classList.contains('buyWithBalance');
  };
  if (!cartRows.length) {
    fragment.querySelector('.cart-modal').replaceChildren(emptyMessage);
    methods.replaceChildren();
  } else {
    const note = document.createElement('p'); note.className = 'cart-note'; note.textContent = 'Kaikki maksut ovat simuloituja. ALV-erittely on testidataa (13,5 %).';
    fragment.querySelector('.cart-modal').append(note);
  }
  showModal(view, fragment);
  if (view.busy) for (const button of modal.querySelectorAll('button')) button.disabled = true;
}
function cartModal() {
  const view = {kind:'cart', collection:R2.cart, balance:null, types:null, busy:false};
  currentView = view; renderCart(view);
  // These two production reads independently rerender the cart and reset terms.
  rpc('cart/getonlinepaymenttypes', []).then(types => { view.types = types; renderCart(view); }).catch(error => message(error.message));
  rpc('cart/checkcurrentuserbalance', []).then(value => { view.balance = Number(value.balance); renderCart(view); }).catch(error => message(error.message));
}
async function refreshCourses() {
  const version = ++snapshotVersion;
  const [latest, own] = await Promise.all([api(prefix + 'apieventgroups'), api(prefix + 'apiusereventgroups')]);
  if (version !== snapshotVersion) return;
  const changed = JSON.stringify(latest) !== JSON.stringify(rows) || JSON.stringify(own) !== JSON.stringify(reservations);
  rows = latest; reservations = own; R2.eventgroups.length = rows.length;
  if (changed) { populateFilters(); renderCourses(); }
  if (readError) message('');
}
const refreshState = () => Promise.all([refreshCourses(), fetchCart()]);
window.Enkora = {getNexusParameter:name => name === 'balance payments enabled'};
window.R2 = {
  session:{isAuthenticated:() => Boolean(user)}, user:{get:key => user?.[key]}, service_group_id:9,
  app:{modal:{currentView:null}},
  eventgroups:{length:0, get:id => {
    const row = rows.find(item => courseId(item) === Number(id));
    return row ? {set:updated => { snapshotVersion++; Object.assign(row, updated); renderCourses(); }} : null;
  }},
  cart:{fetch:fetchCart, toJSON:() => cartRows},
  vent:{trigger:name => { if (name === 'cart:open') return cartModal(); if (name === 'login:open') message('Paikallinen kuvitteellinen tili on jo kirjautunut.'); }}
};
async function start() {
  const response = await fetch('/webshop/resource/testshop/templates.html');
  if (!response.ok) throw Error('Testikaupan HTML-mallien lataus epäonnistui.');
  templates = new DOMParser().parseFromString(await response.text(), 'text/html');
  user = await rpc('user/getcurrentuser', [true, true]);
  for (const selector of ['#locationFilter', '#groupFilter', '#courseSearch']) document.querySelector(selector).addEventListener('input', renderCourses);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') closeModal(); });
  setInterval(async () => {
    if (refreshing) return;
    refreshing = true;
    try {
      await Promise.all([refreshCourses(), cartLoaded ? Promise.resolve() : fetchCart()]);
    } catch (error) { showReadError(error); }
    finally { refreshing = false; }
  }, 2000);
  await refreshState();
}
start().catch(error => {
  showReadError(error);
  if (!rows.length) document.querySelector('#courses').textContent = 'Testikaupan lataus epäonnistui.';
});
