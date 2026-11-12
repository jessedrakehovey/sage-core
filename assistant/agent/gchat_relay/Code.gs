/**
 * The agent's Google Chat relay: the pipe between Google Chat and the PC that runs Mission Control.
 *
 * Google Chat only talks to HTTPS endpoints on port 443 and gives a button tap ~30 s for a synchronous
 * reply. This Apps Script deployment is the Chat app's connection; it works in two modes, chosen by the
 * script properties:
 *
 *   PUSH  (ENDPOINT set)   the original (2026-10-06): every event is forwarded unchanged to Mission
 *                          Control's public /gchat/events (Tailscale Funnel) with X-Sage-Relay, and the
 *                          answer is handed back to Chat. v5 holds events while the PC is away.
 *   PULL  (no ENDPOINT)    sage-core (2026-10-28, the owner: "approval cards without Tailscale"): the PC has
 *                          NO public address. Each event is queued here (CacheService, minutes, never a
 *                          file) and the PC's poller (assistant/scripts/relay_poll.py) fetches it through
 *                          this script's WEB APP URL every few seconds and posts the reply back; the Chat
 *                          handler waits up to WAIT_MS for that reply so the tap still turns into the
 *                          answered card. If the PC is slow, Chat gets the optimistic "got it" card and
 *                          the PC finishes by REST (X-Sage-Late), exactly like v5's held taps.
 *
 * A password typed into the vault dialog (vault_add_submit) is the one event that is NEVER held: if the PC
 * does not answer within the wait, it is dropped from the queue and the owner is told nothing was saved.
 *
 * Script properties: RELAY_SECRET (same as relay_secret in assistant/agent/gchat.local.json), and for push
 * mode ENDPOINT (https://<host>/gchat/events). Deploy twice: as an Add-on (the Chat connection) and as a
 * Web app (the poller's URL; "execute as me", "anyone"). Nothing here decides anything; it is a pipe.
 */
var TAPS_ = { card_answer: 1, card_undo: 1, card_edit_submit: 1, vault_add_submit: 1, vault_add: 1, home_action: 1, form_submit: 1, form_action: 1 };
// never held for later: a dialog or a Home tab nobody is looking at any more (a late reply has no one to reach)
var NEVER_HELD_ = { vault_add_submit: 1, vault_add: 1, card_edit: 1, onAppHome: 1, home_action: 1, onAppCommand: 1, form_open: 1 };
var HOLD_PREFIX_ = 'held_';
var WAIT_MS = 22000;          // how long the Chat handler waits for the PC's reply (Chat allows ~30 s)
var POLL_STEP_MS = 400;
var EVENT_TTL_S = 600;        // a queued event lives this long in the cache (10 min; a late tap is still acted on)
var SECRET_TTL_S = 90;        // a vault submit lives this long and never longer
var QUEUE_KEY = 'queue_ids';

function props_() { return PropertiesService.getScriptProperties(); }
function mode_() { return props_().getProperty('ENDPOINT') ? 'push' : 'pull'; }
function secret_() { return props_().getProperty('RELAY_SECRET') || ''; }
function cache_() { return CacheService.getScriptCache(); }

function say_(text) {
  return { hostAppDataAction: { chatDataAction: { createMessageAction: { message: { text: text } } } } };
}
function escape_(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function closeDialog_(text) {
  return { action: { navigations: [{ endNavigation: { action: 'CLOSE_DIALOG' } }], notification: { text: text } } };
}

/** Only what Mission Control reads (formInputs travel inside commonEventObject). */
function slim_(event) {
  var chat = event.chat || {};
  var out = { relayFunction: event.relayFunction || '', commonEventObject: event.commonEventObject || {}, chat: {} };
  if (chat.user) out.chat.user = chat.user;
  if (chat.buttonClickedPayload) {
    var m = chat.buttonClickedPayload.message || {};
    out.chat.buttonClickedPayload = { message: { name: m.name, space: m.space, thread: m.thread } };
    if (chat.buttonClickedPayload.space) out.chat.buttonClickedPayload.space = chat.buttonClickedPayload.space;
  }
  if (chat.messagePayload) {
    var mm = chat.messagePayload.message || {};
    out.chat.messagePayload = { space: chat.messagePayload.space, message: {
      name: mm.name, text: String(mm.text || '').slice(0, 6000), argumentText: String(mm.argumentText || '').slice(0, 6000),
      sender: mm.sender, space: mm.space, thread: mm.thread, createTime: mm.createTime,
      attachment: mm.attachment } };
  }
  if (chat.addedToSpacePayload) out.chat.addedToSpacePayload = chat.addedToSpacePayload;
  if (chat.appCommandPayload) {
    // a registered slash or quick command (2026-11-04): its id, and the message it rode in on
    var cp = chat.appCommandPayload, cm = cp.message || {};
    out.chat.appCommandPayload = { appCommandMetadata: cp.appCommandMetadata || {}, space: cp.space, message: {
      name: cm.name, text: String(cm.text || '').slice(0, 2000), argumentText: String(cm.argumentText || '').slice(0, 2000),
      sender: cm.sender, space: cm.space, thread: cm.thread, createTime: cm.createTime } };
  }
  if (chat.eventTime) out.chat.eventTime = chat.eventTime;
  return out;
}

/** The Home tab's card while the PC is slow (2026-11-04): a line, and a chip that asks again. */
function homeWaiting_(update) {
  var card = { sections: [{ widgets: [
    { textParagraph: { text: 'My computer is slow to answer. Tap Desk again in a moment.' } },
    { chipList: { chips: [{ label: 'Desk', onClick: { action: { function: 'home_action',
      parameters: [{ key: 'do', value: 'desk' }, { key: 'ctx', value: 'home' }] } } }] } } ] }] };
  var nav = update ? { updateCard: card } : { pushCard: card };
  return { action: { navigations: [nav] } };
}

/** The reply Chat gets when the PC did not answer in time: the choice is shown as taken; the PC finishes later. */
function optimistic_(event, fn) {
  if (fn === 'card_edit') return say_('The agent\'s computer is busy. Tap Edit again in a minute.');
  if (fn === 'vault_add') return say_('The agent\'s computer is busy. Tap Enter login again in a minute.');
  if (fn === 'vault_add_submit') return closeDialog_('The agent\'s computer did not answer, so NOTHING was saved. Try again in a minute.');
  if (fn === 'card_answer') {
    var p = (event.commonEventObject || {}).parameters || {};
    var choice = String(p.option || '').replace(/\s*\(Recommended\)\s*/, '');
    return {
      hostAppDataAction: { chatDataAction: { updateMessageAction: { message: { cardsV2: [{
        cardId: String(p.qid || 'held'),
        card: { sections: [{ widgets: [{ textParagraph: { text:
          '<b>' + escape_(choice) + '</b> - got it.<br>This goes through the moment the agent\'s computer picks it up. ' +
          'If it can\'t, you\'ll get a fresh card.' } }] }] },
      }] } } } },
    };
  }
  if (fn === 'card_edit_submit') return closeDialog_('Got it. The agent will send this the moment its computer picks it up.');
  if (fn === 'card_undo') return say_('Got the Undo. The agent will apply it the moment its computer picks it up.');
  if (fn === 'onAppHome') return homeWaiting_(false);
  if (fn === 'home_action') return homeWaiting_(true);
  if (fn === 'onAppCommand') return say_('My computer is slow to answer; try that again in a moment.');
  if (fn === 'form_action') {
    // a setup button (a job the agent runs, 2026-11-04): the tap is taken; the PC redraws this card when it picks it up
    return { hostAppDataAction: { chatDataAction: { updateMessageAction: { message: { cardsV2: [{
      cardId: 'job-' + String(((event.commonEventObject || {}).parameters || {}).rid || 'setup'),
      card: { header: { title: 'Got it', subtitle: 'working' }, sections: [{ widgets: [{ textParagraph: { text:
        'My computer is picking this up. This card updates itself when it is done; if it has not changed in a few minutes, tap again.' } }] }] },
    }] } } } } };
  }
  if (fn === 'form_open') return say_('The agent\'s computer is busy. Tap the button again in a minute.');
  if (fn === 'form_submit') return closeDialog_('Got it. The card says when it is stored; if it does not change in a few minutes, enter it again.');
  return {};                                     // a message: answered when the PC reads it
}

// ------------------------------------------------------------------------------------------ PUSH mode (2026-10-06, v5)
function post_(endpoint, secret, event, heldAt) {
  var headers = { 'X-Sage-Relay': secret };
  if (heldAt) headers['X-Sage-Late'] = String(heldAt);
  return UrlFetchApp.fetch(endpoint, {
    method: 'post', contentType: 'application/json', headers: headers,
    payload: JSON.stringify(event || {}), muteHttpExceptions: true, followRedirects: false,
  });
}

function push_(event) {
  var endpoint = props_().getProperty('ENDPOINT');
  var secret = secret_();
  var fn = (event && event.relayFunction) || '';
  var tries = TAPS_[fn] || !fn ? 3 : 1;
  var last = '';
  for (var i = 0; i < tries; i++) {
    if (i) Utilities.sleep(1500);
    try {
      var resp = post_(endpoint, secret, event, 0);
      var code = resp.getResponseCode();
      if (code === 200) {
        var out = JSON.parse(resp.getContentText() || '{}');
        try { flushQueue(); } catch (e) { console.error('flush: ' + e); }
        return out;
      }
      last = code + ': ' + resp.getContentText().slice(0, 200);
      if (code === 401 || code === 403) break;
    } catch (e) {
      last = String(e);
    }
  }
  console.error('relay: Mission Control unreachable (' + last + ')');
  if (!NEVER_HELD_[fn]) hold_(event);           // a password, a dialog or a Home tab is never held
  return optimistic_(event, fn);
}

function hold_(event) {
  try {
    var key = HOLD_PREFIX_ + Date.now() + '_' + Math.floor(Math.random() * 1e6);
    var s = JSON.stringify(slim_(event));
    if (s.length > 8500) {
      var e2 = slim_(event);
      if (e2.chat.messagePayload) {
        e2.chat.messagePayload.message.text = e2.chat.messagePayload.message.text.slice(0, 2500);
        e2.chat.messagePayload.message.argumentText = '';
        delete e2.chat.messagePayload.message.attachment;
      }
      s = JSON.stringify(e2).slice(0, 8900);
    }
    props_().setProperty(key, s);
  } catch (e) {
    console.error('hold: ' + e);
  }
}

/** Push mode: hand every held event to Mission Control, oldest first (minute trigger + after any event that got through). */
function flushQueue() {
  if (mode_() !== 'push') return;
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return;
  try {
    var all = props_().getProperties();
    var keys = Object.keys(all).filter(function (k) { return k.indexOf(HOLD_PREFIX_) === 0; }).sort();
    if (!keys.length) return;
    for (var i = 0; i < keys.length; i++) {
      var heldAt = Number(keys[i].split('_')[1]) || Date.now();
      var resp;
      try { resp = post_(all.ENDPOINT, all.RELAY_SECRET, JSON.parse(all[keys[i]]), heldAt); } catch (e) { return; }
      if (resp.getResponseCode() !== 200) return;
      props_().deleteProperty(keys[i]);
    }
  } finally {
    lock.releaseLock();
  }
}

function installFlushTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'flushQueue') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('flushQueue').timeBased().everyMinutes(1).create();
}

// ------------------------------------------------------------------------------------------ PULL mode (sage-core, 2026-10-28)
function queueIds_() {
  try { return JSON.parse(cache_().get(QUEUE_KEY) || '[]'); } catch (e) { return []; }
}
function setQueueIds_(ids) { cache_().put(QUEUE_KEY, JSON.stringify(ids), EVENT_TTL_S); }

/** The queue's lock. waitLock THROWS when another run holds it too long, and that error reached the owner as
 *  "Unable to process request" while the tap still went through later (2026-11-04). tryLock never throws: a run that
 *  cannot get the lock in time goes on without it (a rare lost update beats a certain error). -> the lock, or null. */
function lock_(ms) {
  try {
    var lock = LockService.getScriptLock();
    return lock.tryLock(ms) ? lock : null;
  } catch (e) { console.error('lock: ' + e); return null; }
}
function unlock_(lock) { try { if (lock) lock.releaseLock(); } catch (e) { console.error('unlock: ' + e); } }

function enqueue_(event, fn) {
  var id = 'e' + Date.now() + '_' + Math.floor(Math.random() * 1e6);
  var ttl = fn === 'vault_add_submit' ? SECRET_TTL_S : EVENT_TTL_S;
  var lock = lock_(8000);
  try {
    cache_().put('ev_' + id, JSON.stringify({ id: id, at: Date.now(), fn: fn, event: slim_(event) }), ttl);
    var ids = queueIds_();
    ids.push(id);
    setQueueIds_(ids);
  } finally {
    unlock_(lock);
  }
  return id;
}

function dequeue_(id) {
  var lock = lock_(4000);
  try {
    cache_().remove('ev_' + id);      // gone even if the id list is not rewritten: the poll skips an id with no event
    setQueueIds_(queueIds_().filter(function (x) { return x !== id; }));
  } catch (e) {
    console.error('dequeue: ' + e);
  } finally {
    unlock_(lock);
  }
}

var LEASE_S = 12;             // a handed-over event stays queued until the PC acks it; past this it is handed over again
                              // (40 until 2026-11-04: a poll whose ANSWER was lost kept the tap from the next poll past Chat's wait)

/** The PC's poll: up to `max` queued events, oldest first. LEASED, not removed (2026-11-04): a poll whose answer never
 *  reached the PC (its read timed out while Apps Script was slow) used to lose the tap for good - the owner saw
 *  "Unable to process request" and nothing ever ran. Now the PC acks each event (op=ack) once Mission Control took it;
 *  an un-acked event is handed over again after LEASE_S. The PC skips an id it already handled. */
function popEvents_(max) {
  var lock = lock_(3000);
  if (!lock) return [];                        // busy: the PC asks again in a moment
  try {
    var ids = queueIds_(), out = [], keep = [], now = Date.now();
    for (var i = 0; i < ids.length; i++) {
      var raw = cache_().get('ev_' + ids[i]);
      if (!raw) continue;                                   // expired: gone for good
      keep.push(ids[i]);
      if (out.length >= max) continue;
      var leased = Number(cache_().get('lease_' + ids[i]) || 0);
      if (leased && now - leased < LEASE_S * 1000) continue;
      cache_().put('lease_' + ids[i], String(now), LEASE_S + 5);
      out.push(JSON.parse(raw));
    }
    setQueueIds_(keep);
    return out;
  } finally {
    unlock_(lock);
  }
}

/** The PC took these events: drop them from the queue. */
function ackEvents_(ids) {
  var n = 0;
  for (var i = 0; i < (ids || []).length; i++) {
    try { dequeue_(String(ids[i])); cache_().remove('lease_' + ids[i]); n++; } catch (e) { console.error('ack: ' + e); }
  }
  return n;
}

function pull_(event) {
  var fn = (event && event.relayFunction) || '';
  if (!secret_()) return say_('Relay not configured (RELAY_SECRET).');
  var id = enqueue_(event, fn);
  var deadline = Date.now() + WAIT_MS;
  while (Date.now() < deadline) {
    Utilities.sleep(POLL_STEP_MS);
    var r = cache_().get('reply_' + id);
    if (r) {
      cache_().remove('reply_' + id);
      try { return JSON.parse(r); } catch (e) { return {}; }
    }
  }
  if (NEVER_HELD_[fn]) dequeue_(id);             // never held: a password, a dialog or a Home tab nobody can answer late
  return optimistic_(event, fn);                 // the rest stays queued; the poller delivers it as X-Sage-Late
}

/** Every Chat event comes through here. Whatever goes wrong inside the relay, Chat gets a real answer (the same one
 *  as when the PC is slow), never the runtime's "Unable to process request" (2026-11-04). */
function relay_(event) {
  try {
    return mode_() === 'push' ? push_(event) : pull_(event);
  } catch (e) {
    console.error('relay: ' + ((event && event.relayFunction) || 'message') + ': ' + e);
    try { return optimistic_(event, (event && event.relayFunction) || ''); } catch (e2) { return {}; }
  }
}

/** Web app: the PC side. GET ?secret=..&op=health|poll ; POST {secret, op:"reply", id, body}. */
function doGet(e) {
  var p = (e && e.parameter) || {};
  if (!secret_() || String(p.secret || '') !== secret_()) return json_({ error: 'unauthorized' });
  if (p.op === 'health') return json_({ ok: true, mode: mode_(), pending: queueIds_().length, t: Date.now() });
  if (p.op === 'poll') return json_({ ok: true, events: popEvents_(Number(p.max) || 10), t: Date.now() });
  return json_({ error: 'unknown op' });
}

function doPost(e) {
  var body = {};
  try { body = JSON.parse((e && e.postData && e.postData.contents) || '{}'); } catch (err) { return json_({ error: 'bad json' }); }
  if (!secret_() || String(body.secret || '') !== secret_()) return json_({ error: 'unauthorized' });
  if (body.op === 'reply' && body.id) {
    cache_().put('reply_' + body.id, JSON.stringify(body.body || {}), 60);
    return json_({ ok: true });
  }
  if (body.op === 'ack') return json_({ ok: true, acked: ackEvents_(body.ids || []) });
  if (body.op === 'poll') return json_({ ok: true, events: popEvents_(Number(body.max) || 10), t: Date.now() });
  return json_({ error: 'unknown op' });
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// ------------------------------------------------------------------------------------------ Chat triggers
// (the names are set in Chat API -> Configuration -> Triggers)
function onMessage(event)          { return relay_(event); }
function onAddedToSpace(event)     { return relay_(event); }
function onRemovedFromSpace(event) { return relay_(event); }
function onAppCommand(event)       { event.relayFunction = 'onAppCommand'; return relay_(event); }
// the Home tab (2026-11-04): Chat API -> Configuration -> Support App Home, trigger function onAppHome
function onAppHome(event)          { event.relayFunction = 'onAppHome'; return relay_(event); }
function home_action(event)        { event.relayFunction = 'home_action'; return relay_(event); }

// Button taps and dialogs: the add-on runtime calls the function named in the button's onClick action, so
// these names must match chat_push.py's buttons.
function card_answer(event)      { event.relayFunction = 'card_answer'; return relay_(event); }
function card_undo(event)        { event.relayFunction = 'card_undo'; return relay_(event); }
function card_edit(event)        { event.relayFunction = 'card_edit'; return relay_(event); }
function card_edit_submit(event) { event.relayFunction = 'card_edit_submit'; return relay_(event); }
function vault_add(event)        { event.relayFunction = 'vault_add'; return relay_(event); }
function vault_add_submit(event) { event.relayFunction = 'vault_add_submit'; return relay_(event); }
// the data-entry forms and the setup buttons (scripts/chat_forms.py, 2026-11-01 / 2026-11-04): a connection's card opens
// a dialog (form_open), its submit stores the values (form_submit), a job button runs the job (form_action). The add-on
// runtime dispatches a tap to the function NAMED in the button, so a name missing here fails before anything reaches
// the PC ("Unable to process request", the owner 2026-11-04).
function form_open(event)        { event.relayFunction = 'form_open'; return relay_(event); }
function form_submit(event)      { event.relayFunction = 'form_submit'; return relay_(event); }
function form_action(event)      { event.relayFunction = 'form_action'; return relay_(event); }

/** Push mode only: run once by hand in the editor to prove Apps Script can reach the endpoint. */
function pingTest() {
  var endpoint = props_().getProperty('ENDPOINT') || '';
  if (!endpoint) { console.log('pull mode: nothing to ping; use the web app URL ?op=health&secret=... from the PC'); return; }
  var url = endpoint.replace(/\/events$/, '/ping') + '?src=appsscript';
  var r = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
  console.log(r.getResponseCode() + ' ' + r.getContentText());
}
