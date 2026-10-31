(function () {
  'use strict';

  var POLL_MS = 3000, MAX_BACKOFF_MS = 20000, FETCH_TIMEOUT_MS = 8000;
  var FOCUS_ZOOM = 17, ANIMATE_MS = 2500, SNAP_METRES = 2000;
  var LIVE_S = 30, RECENT_S = 300;
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---- identity: the Android app passes the device token + own public id in the URL fragment
  var auth = { token: '', me: '' };
  function readAuth() {
    var frag = new URLSearchParams((location.hash || '').replace(/^#/, ''));
    var t = frag.get('t'), me = frag.get('me');
    try {
      if (t) sessionStorage.setItem('sumo_t', t); else t = sessionStorage.getItem('sumo_t');
      if (me) sessionStorage.setItem('sumo_me', me); else me = sessionStorage.getItem('sumo_me');
    } catch (e) { /* storage unavailable: keep values in memory only */ }
    auth.token = t || ''; auth.me = me || '';
    if (location.hash) history.replaceState(null, '', location.pathname + location.search);
  }
  readAuth();

  // ---- map
  var map = L.map('map', { worldCopyJump: true }).setView([20, 0], 2);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);

  var people = new Map();      // id -> record
  var selectedId = null;       // highlighted person
  var following = false;       // keep selected person centred until the user drags the map
  var accuracyCircle = null;
  var didInitialFit = false;
  var order = [];

  var els = {
    list: document.getElementById('people'),
    empty: document.getElementById('empty'),
    summary: document.getElementById('summary'),
    conn: document.getElementById('conn'),
    follow: document.getElementById('follow'),
    followText: document.getElementById('followText'),
    followStop: document.getElementById('followStop'),
    fitAll: document.getElementById('fitAll')
  };

  // ---- helpers
  function initials(name) {
    var words = String(name || '?').trim().split(/\s+/).filter(Boolean);
    var first = Array.from(words[0] || '?')[0] || '?';
    var second = words.length > 1 ? (Array.from(words[1])[0] || '') : '';
    return (first + second).toUpperCase();
  }
  function ageOf(rec) { return rec.age0 + (Date.now() - rec.fetchedAt) / 1000; }
  function statusOf(age) { return age < LIVE_S ? 'live' : age < RECENT_S ? 'recent' : 'stale'; }
  function fmtAge(s) {
    s = Math.max(0, Math.round(s));
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm ago';
    return Math.floor(s / 86400) + 'd ago';
  }
  function statusText(rec) {
    var age = ageOf(rec), st = statusOf(age);
    var acc = rec.accuracy > 0 ? ' · ±' + Math.round(rec.accuracy) + ' m' : '';
    if (st === 'live') return 'Live · ' + fmtAge(age) + acc;
    if (st === 'recent') return 'Updated ' + fmtAge(age) + acc;
    return 'Last seen ' + fmtAge(age) + ' — may be out of date';
  }
  function validCoord(m) {
    return typeof m.latitude === 'number' && typeof m.longitude === 'number' &&
      isFinite(m.latitude) && isFinite(m.longitude) &&
      Math.abs(m.latitude) <= 90 && Math.abs(m.longitude) <= 180;
  }

  // ---- list item + marker creation (all text via textContent: no HTML injection)
  function makeListItem(id) {
    var li = document.createElement('li');
    li.className = 'person';
    li.dataset.id = id;
    li.tabIndex = 0;
    li.setAttribute('role', 'button');
    var av = document.createElement('span'); av.className = 'avatar';
    var info = document.createElement('span'); info.className = 'info';
    var nm = document.createElement('span'); nm.className = 'pname';
    var st = document.createElement('span'); st.className = 'pstatus';
    var dot = document.createElement('i'); dot.className = 'dot';
    var stText = document.createElement('span');
    st.append(dot, stText);
    info.append(nm, st);
    var loc = document.createElement('span'); loc.className = 'locate'; loc.textContent = 'Locate';
    li.append(av, info, loc);
    return { li: li, av: av, nm: nm, dot: dot, stText: stText };
  }

  function makeMarker(rec, latlng) {
    var pin = document.createElement('div'); pin.className = 'pin';
    var badge = document.createElement('div'); badge.className = 'badge';
    var label = document.createElement('div'); label.className = 'label';
    pin.append(badge, label);
    var icon = L.divIcon({ className: 'pin-wrap', html: pin, iconSize: [120, 64], iconAnchor: [60, 20], popupAnchor: [0, -22] });
    var marker = L.marker(latlng, { icon: icon, riseOnHover: true, keyboard: false }).addTo(map);
    marker.bindPopup(document.createElement('div'));
    marker.on('click', function () { select(rec.id); });
    rec.pin = pin; rec.badge = badge; rec.label = label;
    return marker;
  }

  function popupContent(rec) {
    var box = document.createElement('div');
    var a = document.createElement('div'); a.className = 'popup-name'; a.textContent = rec.name + (rec.isMe ? ' (You)' : '');
    var b = document.createElement('div'); b.className = 'popup-sub'; b.textContent = statusText(rec);
    box.append(a, b);
    return box;
  }

  // ---- smooth movement between *real* fixes (linear interpolation, never invented positions)
  function moveMarker(rec, target) {
    var from = rec.marker.getLatLng();
    var dist = map.distance(from, target);
    if (rec.raf) { cancelAnimationFrame(rec.raf); rec.raf = 0; }
    if (dist < 0.3) return;
    if (reduceMotion || dist > SNAP_METRES || document.hidden) { setMarkerPos(rec, target); return; }
    var t0 = performance.now();
    (function step(now) {
      var k = Math.min(1, (now - t0) / ANIMATE_MS);
      setMarkerPos(rec, L.latLng(from.lat + (target.lat - from.lat) * k, from.lng + (target.lng - from.lng) * k));
      rec.raf = k < 1 ? requestAnimationFrame(step) : 0;
    })(t0);
  }
  function setMarkerPos(rec, ll) {
    rec.marker.setLatLng(ll);
    if (selectedId === rec.id) {
      if (accuracyCircle) accuracyCircle.setLatLng(ll);
      if (following) map.panTo(ll, { animate: false });
    }
  }

  // ---- selection / "point out this person"
  function select(id) {
    var rec = people.get(id);
    if (!rec) return;
    selectedId = id;
    following = true;
    var ll = rec.marker.getLatLng();
    map.setView(ll, Math.max(map.getZoom(), FOCUS_ZOOM), { animate: !reduceMotion, duration: 0.8 });
    rec.marker.setPopupContent(popupContent(rec));
    rec.marker.openPopup();
    updateSelectionUI();
    rec.item.li.scrollIntoView({ block: 'nearest' });
  }
  function updateSelectionUI() {
    people.forEach(function (rec, id) {
      var on = id === selectedId;
      rec.item.li.classList.toggle('sel', on);
      rec.item.li.setAttribute('aria-pressed', on ? 'true' : 'false');
      rec.pin.classList.toggle('sel', on);
      rec.marker.setZIndexOffset(on ? 1000 : 0);
    });
    var rec = people.get(selectedId);
    if (accuracyCircle) { map.removeLayer(accuracyCircle); accuracyCircle = null; }
    if (rec && rec.accuracy > 0) {
      try {  // purely decorative: must never break selecting a person
        accuracyCircle = L.circle(rec.marker.getLatLng(), {
          radius: rec.accuracy, color: '#8a004f', weight: 1, fillColor: '#8a004f', fillOpacity: 0.08, interactive: false
        }).addTo(map);
      } catch (e) { accuracyCircle = null; }
    }
    if (rec && following) {
      els.followText.textContent = 'Following ' + rec.name;
      els.follow.classList.remove('hidden');
    } else {
      els.follow.classList.add('hidden');
    }
  }
  function stopFollowing() { following = false; updateSelectionUI(); }
  els.followStop.addEventListener('click', stopFollowing);
  map.on('dragstart', function () { if (following) stopFollowing(); });

  // one delegated handler: works for items added later and has no inline-script id quoting problems
  els.list.addEventListener('click', function (e) {
    var li = e.target.closest('.person');
    if (li) select(li.dataset.id);
  });
  els.list.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    var li = e.target.closest('.person');
    if (li) { e.preventDefault(); select(li.dataset.id); }
  });

  function fitAll() {
    var pts = [];
    people.forEach(function (rec) { pts.push(rec.marker.getLatLng()); });
    following = false; updateSelectionUI();
    if (!pts.length) return;
    if (pts.length === 1) map.setView(pts[0], 16, { animate: !reduceMotion });
    else map.fitBounds(L.latLngBounds(pts), { padding: [60, 60], maxZoom: 16, animate: !reduceMotion });
  }
  els.fitAll.addEventListener('click', fitAll);

  // ---- apply server data
  function upsert(m) {
    var rec = people.get(m.id);
    var ll = L.latLng(m.latitude, m.longitude);
    var isNew = !rec;
    if (isNew) {
      rec = { id: m.id, item: makeListItem(m.id), raf: 0 };
      rec.marker = makeMarker(rec, ll);
      people.set(m.id, rec);
    }
    rec.name = m.name || 'Unknown';
    rec.isMe = !!m.is_me || (!!auth.me && m.id === auth.me);
    rec.accuracy = typeof m.accuracy === 'number' ? m.accuracy : 0;
    rec.age0 = typeof m.age_seconds === 'number' ? m.age_seconds : 0;
    rec.fetchedAt = Date.now();
    if (!isNew) moveMarker(rec, ll);
    if (selectedId === m.id && accuracyCircle) accuracyCircle.setRadius(Math.max(rec.accuracy, 1));
    return isNew;
  }

  function renderStatuses() {
    var live = 0;
    people.forEach(function (rec) {
      var st = statusOf(ageOf(rec));
      if (st === 'live') live++;
      rec.item.av.textContent = initials(rec.name);
      rec.item.nm.textContent = rec.name;
      if (rec.isMe) { var you = document.createElement('em'); you.textContent = ' (You)'; rec.item.nm.append(you); }
      rec.item.dot.className = 'dot ' + st;
      rec.item.stText.textContent = statusText(rec);
      rec.item.li.classList.toggle('stale', st === 'stale');
      rec.badge.textContent = initials(rec.name);
      rec.label.textContent = rec.name;
      rec.pin.classList.toggle('stale', st === 'stale');
      if (rec.marker.isPopupOpen()) rec.marker.setPopupContent(popupContent(rec));
    });
    var n = people.size;
    els.summary.textContent = n === 0 ? 'No one sharing' : n + (n === 1 ? ' person' : ' people') + ' · ' + live + ' live';
    els.empty.classList.toggle('hidden', n !== 0);
  }

  function reorderList() {
    var ids = Array.from(people.keys()).sort(function (a, b) {
      var A = people.get(a), B = people.get(b);
      if (A.isMe !== B.isMe) return A.isMe ? -1 : 1;
      return A.name.localeCompare(B.name, undefined, { sensitivity: 'base' });
    });
    if (ids.length === order.length && ids.every(function (v, i) { return v === order[i]; })) return;
    order = ids;
    ids.forEach(function (id) { els.list.appendChild(people.get(id).item.li); });
  }

  function apply(data) {
    var seen = new Set();
    (data.members || []).forEach(function (m) {
      if (!m || typeof m.id !== 'string' || !validCoord(m)) return;
      seen.add(m.id);
      upsert(m);
    });
    people.forEach(function (rec, id) {
      if (seen.has(id)) return;
      if (rec.raf) cancelAnimationFrame(rec.raf);
      map.removeLayer(rec.marker);
      if (rec.item.li.parentNode) rec.item.li.parentNode.removeChild(rec.item.li);
      people.delete(id);
      if (selectedId === id) { selectedId = null; following = false; }
    });
    reorderList();
    renderStatuses();
    updateSelectionUI();
    if (!didInitialFit && people.size) { didInitialFit = true; fitAllQuiet(); }
  }
  function fitAllQuiet() {
    var pts = []; people.forEach(function (r) { pts.push(r.marker.getLatLng()); });
    if (pts.length === 1) map.setView(pts[0], 16, { animate: false });
    else map.fitBounds(L.latLngBounds(pts), { padding: [60, 60], maxZoom: 16, animate: false });
  }

  // ---- connection banner
  function setConn(kind) {
    var msg = {
      offline: 'Offline — showing last known positions',
      error: "Can't reach the Sumo server — retrying…",
      auth: 'Open this map from the Sumo app (your device was not recognised).'
    }[kind];
    if (!msg) { els.conn.classList.add('hidden'); return; }
    els.conn.textContent = msg;
    els.conn.classList.remove('hidden');
  }

  // ---- polling: exactly one timer, no overlapping requests, paused while hidden, backs off on errors
  var timer = 0, inflight = false, failures = 0, authFailed = false;

  function schedule() {
    clearTimeout(timer); timer = 0;
    if (document.hidden) return;
    var delay = authFailed ? 10000 : failures ? Math.min(MAX_BACKOFF_MS, POLL_MS * Math.pow(2, failures)) : POLL_MS;
    timer = setTimeout(poll, delay);
  }

  function poll() {
    if (inflight) return;
    inflight = true;
    var ctrl = new AbortController();
    var killer = setTimeout(function () { ctrl.abort(); }, FETCH_TIMEOUT_MS);
    var headers = auth.token ? { Authorization: 'Bearer ' + auth.token } : {};
    fetch('/api/people', { headers: headers, cache: 'no-store', signal: ctrl.signal })
      .then(function (r) {
        if (r.status === 401) { authFailed = true; setConn('auth'); renderStatuses(); return null; }
        return r.json().then(function (d) {
          if (!d || !d.ok) throw new Error((d && d.error) || 'bad response');
          return d;
        });
      })
      .then(function (d) {
        if (!d) return;
        authFailed = false; failures = 0; setConn(null); apply(d);
      })
      .catch(function () {
        failures = Math.min(failures + 1, 5);
        setConn(navigator.onLine === false ? 'offline' : 'error');
        renderStatuses();
      })
      .then(function () { clearTimeout(killer); inflight = false; schedule(); });
  }

  document.addEventListener('visibilitychange', function () {
    clearTimeout(timer); timer = 0;
    if (!document.hidden) poll();
  });
  window.addEventListener('online', function () { failures = 0; clearTimeout(timer); poll(); });
  window.addEventListener('offline', function () { setConn('offline'); });
  window.addEventListener('hashchange', function () { readAuth(); authFailed = false; failures = 0; clearTimeout(timer); poll(); });

  poll();
})();
