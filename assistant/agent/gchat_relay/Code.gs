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
var TAPS_ = { card_answer: 1, card_undo: 1, card_edit_submit: 1, vault_add_submit: 1, vault_add: 1 };
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
  return out;
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
  if (fn !== 'vault_add_submit' && fn !== 'card_edit' && fn !== 'vault_add') hold_(event);   // a password is never held
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

function enqueue_(event, fn) {
  var id = 'e' + Date.now() + '_' + Math.floor(Math.random() * 1e6);
  var ttl = fn === 'vault_add_submit' ? SECRET_TTL_S : EVENT_TTL_S;
  var lock = LockService.getScriptLock();
  lock.waitLock(5000);
  try {
    cache_().put('ev_' + id, JSON.stringify({ id: id, at: Date.now(), fn: fn, event: slim_(event) }), ttl);
    var ids = queueIds_();
    ids.push(id);
    setQueueIds_(ids);
  } finally {
    lock.releaseLock();
  }
  return id;
}

function dequeue_(id) {
  var lock = LockService.getScriptLock();
  lock.waitLock(5000);
  try {
    cache_().remove('ev_' + id);
    setQueueIds_(queueIds_().filter(function (x) { return x !== id; }));
  } finally {
    lock.releaseLock();
  }
}

/** The PC's poll: up to `max` queued events, oldest first, removed from the queue as they are handed over. */
function popEvents_(max) {
  var lock = LockService.getScriptLock();
  lock.waitLock(5000);
  try {
    var ids = queueIds_();
    var take = ids.slice(0, max), rest = ids.slice(max);
    var out = [];
    for (var i = 0; i < take.length; i++) {
      var raw = cache_().get('ev_' + take[i]);
      if (raw) { out.push(JSON.parse(raw)); cache_().remove('ev_' + take[i]); }
    }
    setQueueIds_(rest);
    return out;
  } finally {
    lock.releaseLock();
  }
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
  if (fn === 'vault_add_submit' || fn === 'vault_add' || fn === 'card_edit') dequeue_(id);   // never held: a password, or a dialog nobody can answer late
  return optimistic_(event, fn);                 // the rest stays queued; the poller delivers it as X-Sage-Late
}

function relay_(event) {
  return mode_() === 'push' ? push_(event) : pull_(event);
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
function onAppCommand(event)       { return relay_(event); }

// Button taps and dialogs: the add-on runtime calls the function named in the button's onClick action, so
// these names must match chat_push.py's buttons.
function card_answer(event)      { event.relayFunction = 'card_answer'; return relay_(event); }
function card_undo(event)        { event.relayFunction = 'card_undo'; return relay_(event); }
function card_edit(event)        { event.relayFunction = 'card_edit'; return relay_(event); }
function card_edit_submit(event) { event.relayFunction = 'card_edit_submit'; return relay_(event); }
function vault_add(event)        { event.relayFunction = 'vault_add'; return relay_(event); }
function vault_add_submit(event) { event.relayFunction = 'vault_add_submit'; return relay_(event); }

/** Push mode only: run once by hand in the editor to prove Apps Script can reach the endpoint. */
function pingTest() {
  var endpoint = props_().getProperty('ENDPOINT') || '';
  if (!endpoint) { console.log('pull mode: nothing to ping; use the web app URL ?op=health&secret=... from the PC'); return; }
  var url = endpoint.replace(/\/events$/, '/ping') + '?src=appsscript';
  var r = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
  console.log(r.getResponseCode() + ' ' + r.getContentText());
}
