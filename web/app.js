'use strict';
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const state = { status: null, bluetooth: null, fleet: null, fleetEvents: [], fleetCursor: null, scans: [], vehicles: [], selected: null, devices: [], busy: false, lastScanSignature: '', view: 'workspace', authenticated: false };
const escapeHTML = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const E = escapeHTML;
const date = (v) => v ? new Date(v).toLocaleString(undefined, {dateStyle:'medium', timeStyle:'short'}) : '';
const fleetDate = (v) => v ? new Date(v).toLocaleString(undefined, {year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',timeZoneName:'short'}) : '';
const safeURL = (v) => {try {const u = new URL(v); return ['http:','https:'].includes(u.protocol) ? u.href : '#';} catch {return '#';}};
let toastTimer, refreshRunning = false;

function toast(message, error=false) { const el = $('#toast'); el.textContent = message; el.classList.toggle('error', error); el.hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.hidden=true, error ? 12000 : 5000); }
async function api(path, body, fileType) {
  return apiRequest(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':fileType || 'application/json','X-Apex-Client':'1'}, body:fileType ? body : JSON.stringify(body)});
}
async function apiRequest(path, options) {
  const response = await fetch('/api' + path, options);
  const data = await response.json();
  if(response.status===401 && path!=='/login')showLogin();
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}
function deleteRecord(path) {
  return apiRequest(path,{method:'DELETE',headers:{'X-Apex-Client':'1'}});
}
function showLogin(changePassword = false, username = '') {
  state.authenticated = false;
  document.body.classList.add('signed-out');
  $('#login-screen').hidden = false;
  $('#login-loading').hidden = true;
  $('#login-form').hidden = changePassword;
  $('#initial-password-form').hidden = !changePassword;
  $('#password-username').value = username;
  $('#sign-out').hidden = true;
  (changePassword ? $('#initial-password-form').elements.password : $('#staff-login')).focus();
}

function signedIn(session) {
  state.authenticated = true;
  state.user = session.user;
  document.body.classList.remove('signed-out');
  document.body.classList.toggle('regular-user', session.user?.role === 'user');
  $('#login-screen').hidden = true;
  $('#sign-out').hidden = !session.required;
  if(location.hash==='#setup/fleet')$('#fleet').scrollIntoView({block:'start'});
  if(session.user?.role==='user' && state.view==='notifications')view('workspace');
}

async function submitAccountForm(event, route, errorId) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button');
  const error = $(errorId);
  error.textContent = '';
  button.disabled = true;
  try {
    await api(route, Object.fromEntries(new FormData(form)));
    form.reset();
    location.reload();
  } catch (failure) {
    error.textContent = failure.message;
  } finally {
    button.disabled = false;
  }
}
$('#login-form').onsubmit = event => submitAccountForm(event, '/login', '#login-error');
$('#initial-password-form').onsubmit = event => submitAccountForm(event, '/password', '#password-error');

async function signOut() {
  try {
    await api('/logout', {});
    localStorage.removeItem('apex-vehicle');
    location.reload();
  } catch (error) {
    toast(error.message, true);
  }
}
$('#sign-out').onclick = signOut;
$('#password-back').onclick = signOut;
async function action(button, task) {
  if (state.busy) return;
  state.busy = true;
  const old = button.textContent; button.textContent = 'Working…'; button.disabled = true;
  try { await task(); } catch (e) {toast(e.message, true);}
  finally {state.busy=false; button.textContent=old; button.disabled=false; await refresh();}
}
function view(name) {
  const [page, target] = name.split('/');
  const fleetSetup = page === 'fleet' || (page === 'setup' && target === 'fleet');
  const returnToSetup = !fleetSetup && page === 'setup' && location.hash === '#setup/fleet';
  name = fleetSetup ? 'setup' : document.getElementById(page)?.classList.contains('view') ? page : 'workspace';
  if(name==='notifications' && state.user?.role==='user')name='workspace';
  state.viewTarget = ['scanners', 'dossiers'].includes(name) && /^[0-9a-f-]{36}$/.test(target || '') ? target : null;
  state.view=name; $$('.view').forEach(el=>el.classList.toggle('active',el.id===name));
  $$('.nav').forEach(el=>el.classList.toggle('active',el.dataset.view===name));
  $('#view-name').textContent = ({workspace:'Overview',connection:'Adapter & vehicle',dossiers:'Vehicle Scans',scanners:'Scanner Inventory',special:'Special Functions',library:'DTC reference library',notifications:'Notifications',setup:'Setup'})[name];
  const hash = fleetSetup ? '#setup/fleet' : '#'+name+(state.viewTarget ? '/'+state.viewTarget : '');
  if(location.hash!==hash)history.replaceState(null,'',hash);
  document.dispatchEvent(new CustomEvent('obd-view',{detail:name}));
  if(fleetSetup && state.authenticated)$('#fleet').scrollIntoView({block:'start'});
  if(returnToSetup)$('#setup').scrollIntoView({block:'start'});
}
document.addEventListener('click',e=>{const target=e.target.closest('[data-view]');if(target)view(target.dataset.view);});
window.addEventListener('hashchange',()=>view(location.hash.slice(1)));

function readingsHTML(values) {
  return (values || []).map(v=>`<div class="reading"><span>${E(v.label || 'PID ' + v.pid)}</span><strong>${E(v.value)}</strong><small>${E(v.unit)}</small><span class="ecu">ECU ${E(v.ecu || 'unknown')} · PID ${E(v.pid)}</span></div>`).join('');
}
function reportHTML(report) {
  const chars=Array.from(report.text || '');
  const annotations=(report.annotations||[]).filter(a=>Number.isInteger(a.start)&&Number.isInteger(a.end)&&a.start>=0&&a.end>a.start&&a.end<=chars.length&&safeURL(a.url)!=='#').sort((a,b)=>a.start-b.start);
  let html='',offset=0;
  for(const a of annotations){if(a.start<offset)continue;html+=E(chars.slice(offset,a.start).join(''));html+=`<a href="${E(safeURL(a.url))}" target="_blank" rel="noopener noreferrer" title="${E(a.title)}">${E(chars.slice(a.start,a.end).join('')) || '[source]'}</a>`;offset=a.end;}
  return html+E(chars.slice(offset).join(''));
}
function renderStatus() {
  const {adapter:a,ai,dependencies,catalog}=state.status;
  const badge=$('#connection-badge'); badge.textContent = a.polling ? 'Polling live values' : a.ready ? 'Diagnostics ready' : a.connected ? 'Adapter connected' : 'Adapter disconnected';
  badge.className='badge'+(a.ready?'':' neutral');
  $('#adapter-summary').textContent=a.ready?'Ready':a.connected?'Connected':'Offline';
  $('#adapter-detail').textContent=a.device || 'Carista EVO · direct Bluetooth';
  $('#ai-summary').textContent=ai.configured?'Available':'Not configured';
  $('#vehicle-count').textContent=state.vehicles.filter(v=>!v.demo).length;
  const sessions=state.vehicles.filter(g=>!g.demo).reduce((n,g)=>n+(g.totalScanCount||g.scans.length),0);
  $('#session-count').textContent=`${sessions} saved scan session${sessions===1?'':'s'}`;
  $('#initialize').disabled=state.busy || !a.connected || a.polling || state.status.automaticScan?.manualPending;
  $('#disconnect').disabled=state.busy || !a.connected;
  $('#discover').disabled=state.busy || a.connected || state.bluetooth?.pairing.status==='pairing';
  $('#scan-button').disabled=state.busy || !a.ready;
  $('#start-poll').disabled=state.busy || !a.ready || !a.scanId || a.polling;
  $('#stop-poll').disabled=state.busy || !a.polling;
  $('#adapter-message').textContent=state.status.automaticScan?.manualPending?'Connected. Initializing and saving a code scan automatically…':a.error || (a.ready ? `${(a.identity||'').replace(/[\r\n]+/g,' ')} · Protocol ${a.protocol} · ${Object.keys(a.supported).length} responding ECU(s)` : a.connected?(a.profile || 'Connected. Automatic code scan will start shortly.'):'Waiting for an adapter.');
  $('#adapter-message').classList.toggle('error-text',!!a.error);
  $('#endpoints').hidden=!a.connected || a.transport!=='ble';
  if(a.connected && a.transport==='ble') {
    const signature=JSON.stringify(a.characteristics);
    if($('#tx').dataset.signature!==signature) {
      const txs=a.characteristics.filter(c=>c.properties.some(p=>['write','write-without-response'].includes(p)));
      const rxs=a.characteristics.filter(c=>c.properties.some(p=>['notify','indicate'].includes(p)));
      const options=chars=>chars.map(c=>`<option value="${c.handle}">${E(c.uuid)} (handle ${c.handle})</option>`).join('');
      $('#tx').innerHTML=options(txs);$('#rx').innerHTML=options(rxs);
      // Prefer a matching service; users can inspect and choose actual endpoints.
      const candidates=txs.flatMap(t=>rxs.filter(r=>r.service===t.service).map(r=>({t,r})));
      const pair=candidates.find(p=>p.t.uuid.startsWith('00002af1-')&&p.r.uuid.startsWith('00002af0-')) || candidates[0];
      if(pair){$('#tx').value=pair.t.handle;$('#rx').value=pair.r.handle;}
      $('#tx').dataset.signature=signature;
      $('#gatt-list').textContent=JSON.stringify(a.characteristics,null,2);
    }
  } else {delete $('#tx').dataset.signature;}
  const supported=new Set(Object.values(a.supported).flat());
  if(a.ready)supported.add('RV');
  const sig=JSON.stringify([...supported]);
  if($('#pid-list').dataset.signature!==sig) {
    const saved=new Set(a.selectedPids.length?a.selectedPids:['0C','05','0D','42','5B','RV']);
    const available=catalog.filter(c=>supported.has(c.pid));
    $('#pid-list').innerHTML=available.length?available.map(c=>`<label class="check"><input type="checkbox" name="pid" value="${E(c.pid)}" ${saved.has(c.pid)?'checked':''}> ${E(c.label)} (${E(c.unit)})</label>`).join(''):'<p class="muted">No vehicle capabilities discovered yet.</p>';
    $('#pid-list').dataset.signature=sig;
  }
  $('#bridge-values').innerHTML=readingsHTML(a.latest);
  $('#poll-stamp').textContent=a.lastSampleAt?`Last cycle: ${date(a.lastSampleAt)} · ${a.polling?'polling':'stopped'}`:'';
  $('#dependency-status').textContent=Object.values(dependencies).every(Boolean)?'Python Bluetooth LE and serial libraries are installed.':'Bluetooth libraries are missing. '+state.status.installHint;
  if(!$('#ai-form').dataset.dirty) {
    renderAIModel(ai.model);
    $('#auto-research').checked=ai.autoResearch;
  }
}
function renderDevices() {
  const signature=JSON.stringify([state.devices,state.status?.adapter.connected,state.bluetooth?.pairing.status,state.busy]);
  if($('#device-list').dataset.signature===signature)return;
  $('#device-list').dataset.signature=signature;
  const disabled=state.busy || state.status?.adapter.connected || state.bluetooth?.pairing.status==='pairing';
  $('#device-list').innerHTML=state.devices.map((d,i)=>`<div class="device"><div><strong>${E(d.name)}${d.saved?' · Saved receiver':''}</strong><p>${E(d.transport==='ble'?'Bluetooth LE':'Serial')} · ${E(d.id)}${d.rssi!=null?' · '+E(d.rssi)+' dBm':''}</p><p>${E(d.hint||'')}${d.paired?' · Paired':''}${d.trusted?' · Remembered by Pi':''}</p>${d.uuids?.length?`<details><summary>Advertised services</summary><p>${d.uuids.map(E).join('<br>')}</p></details>`:''}</div><div class="receiver-actions">${state.bluetooth?.available&&d.transport==='ble'?`<button class="button secondary" data-admin-only data-fleet="${i}" ${disabled?'disabled':''}>Add to fleet</button><button class="button secondary" data-pair="${i}" ${disabled?'disabled':''}>Pair (optional)</button>`:''}<button class="button secondary" data-device="${i}" ${disabled?'disabled':''}>Connect & scan</button></div></div>`).join('')||'<p class="muted">Discover nearby receivers, then choose your own OBD adapter.</p>';
}
function renderFleet() {
  const f=state.fleet;if(!f)return;
  $('#fleet-count').textContent=`${f.readers.length} / ${f.capacity} fleet readers`;
  $('#fleet-toggle').textContent=f.settings.enabled?'Pause automatic scans':'Resume automatic scans';
  $('#fleet-worker').textContent=!f.settings.enabled?'Paused':f.busyReader?'Scanning '+f.busyReader:f.running?'Monitoring':'Waiting for service';
  $('#fleet-error').textContent=[f.lastError,...Object.values(f.radioErrors||{})].filter(Boolean).join(' · ');
  if(!$('#fleet-schedule').dataset.dirty) {
    $('#fleet-interval').value=f.settings.scanIntervalSeconds/60;
    $('#fleet-return-after').value=f.settings.returnAfterSeconds/60;
  }
  const signature=JSON.stringify(f.readers);
  if($('#fleet-readers').dataset.signature!==signature){$('#fleet-readers').dataset.signature=signature;$('#fleet-readers').innerHTML=f.readers.map(r=>`<article class="fleet-reader"><div class="panel-heading"><strong>${E(r.label)}</strong><span class="badge ${r.lastError?'amber':r.scanFresh?'':'neutral'}">${r.scanFresh?'Fresh scan':E(r.lastScanStatus)}${r.lastCodeCount!=null?' · '+r.lastCodeCount+' code(s)':''}</span></div><p class="muted small">${E(r.address)} · ${E(r.presence)}${r.vin?' · VIN '+E(r.vin):' · VIN awaiting vehicle response'}</p><p class="muted small">Last scan: ${r.lastScanAt?E(fleetDate(r.lastScanAt)):'Not yet captured'}${r.lastScanAgeSeconds!=null?' · '+Math.round(r.lastScanAgeSeconds)+' seconds ago':''}</p>${r.lastError?`<p class="error-text">${E(r.lastError)}</p>`:''}<div class="actions">${r.lastScanId?`<a class="button secondary" href="/api/scans/${E(r.lastScanId)}/summary.pdf">Diagnostic PDF</a><a class="text-button" href="/api/scans/${E(r.lastScanId)}/diagnostic">Diagnostic JSON</a>`:''}<button class="text-button" data-admin-only data-remove-reader="${E(r.address)}">Remove reader</button></div></article>`).join('')||'<p class="muted">Discover receivers in Adapter & vehicle, then Add to fleet. A manual connection also registers a Carista and starts its first scan automatically.</p>';}
  const events=state.fleetEvents.slice(-40).reverse();
  $('#fleet-events').innerHTML=events.map(e=>`<div class="fleet-event"><strong>${E(e.type.replaceAll('_',' '))}${e.code?' · '+E(e.code):''}</strong><span class="muted small">${E(fleetDate(e.createdAt))} · ${E(e.address||'Fleet monitor')}${e.ecu?' · ECU '+E(e.ecu):''}${e.status?' · '+E(e.status):''}</span>${e.error?`<p class="error-text">${E(e.error)}</p>`:''}${e.scanId?`<a href="/api/scans/${E(e.scanId)}/summary.pdf">Download diagnostic PDF</a> &middot; <a href="/api/scans/${E(e.scanId)}/diagnostic">Diagnostic JSON</a>`:''}</div>`).join('')||'<p class="muted">Fleet events appear here as readers are detected and scanned.</p>';
}
$('#fleet-toggle').addEventListener('click',e=>action(e.target,async()=>{await api('/fleet/settings',{enabled:!state.fleet.settings.enabled});}));
$('#fleet-schedule').addEventListener('input',e=>{e.currentTarget.dataset.dirty='true';});
$('#fleet-schedule').addEventListener('submit',e=>{
  e.preventDefault();
  const form=e.currentTarget;
  const scanIntervalSeconds=Math.round(Number($('#fleet-interval').value)*60);
  const returnAfterSeconds=Math.round(Number($('#fleet-return-after').value)*60);
  action(form.querySelector('button'),async()=>{
    const settings=await api('/fleet/settings',{scanIntervalSeconds,returnAfterSeconds});
    delete form.dataset.dirty;
    if(state.fleet){state.fleet.settings=settings;renderFleet();}
    toast('Fleet scan schedule saved.');
  });
});
$('#fleet-readers').addEventListener('click',e=>{const b=e.target.closest('[data-remove-reader]');if(b)action(b,async()=>{await api('/fleet/remove',{address:b.dataset.removeReader});toast('Reader removed from automatic scans. Diagnostic history was retained.');});});
function renderBluetooth() {
  const b=state.bluetooth;if(!b)return;
  $('#pi-bluetooth').hidden=!b.available;
  $('#bluetooth-error').textContent=b.error||'';
  const signature=JSON.stringify(b.controllers);
  if($('#bluetooth-controller').dataset.signature!==signature){$('#bluetooth-controller').innerHTML=b.controllers.map(c=>`<option value="${E(c.id)}">${E(c.name)} · ${E(c.address)} · ${c.powered?'On':'Off'}</option>`).join('');$('#bluetooth-controller').value=b.controller||'';$('#bluetooth-controller').dataset.signature=signature;}
  const radio=b.controllers.find(c=>c.id===$('#bluetooth-controller').value);
  $('#bluetooth-controller').disabled=state.busy || state.status?.adapter.connected || b.pairing.status==='pairing';
  $('#bluetooth-power').textContent=radio?.powered?'Turn Bluetooth off':'Turn Bluetooth on';
  $('#bluetooth-power').disabled=state.busy || state.status?.adapter.connected || b.pairing.status==='pairing' || !radio;
  $('#bluetooth-status').textContent=radio?.powered?'Pi radio is on':'Pi radio is off';
  $('#saved-receiver').textContent=b.selected?`Saved receiver: ${b.selected.name||b.selected.id} · ${b.selected.id}`:'';
  const pairing=b.pairing,p=pairing.prompt;
  $('#pairing-panel').hidden=pairing.status==='idle';
  $('#pairing-status').textContent=pairing.error || (pairing.status==='paired'?'Receiver paired. Choose Connect to use it.':pairing.status==='cancelled'?'Pairing cancelled.':`Pairing ${pairing.device||'receiver'}…`);
  $('#pair-cancel').hidden=pairing.status!=='pairing';
  $('#pair-submit').hidden=!p || p.kind==='display';
  $('#pairing-input-label').hidden=!p || !['pin','passkey'].includes(p.kind);
  $('#pairing-instructions').textContent=!p?'If your receiver asks for a PIN or confirmation, it will appear here.':p.kind==='pin'?'Enter the PIN from the receiver label or manual.':p.kind==='passkey'?'Enter the numeric passkey requested by the receiver.':p.kind==='display'?`Enter ${p.passkey} on the receiver.`:`Confirm this is your receiver${p.passkey?' and its code is '+p.passkey:''}${p.service?' (service '+p.service+')':''}.`;
  $('#pair-submit').textContent=p?.kind==='confirm'?'Confirm receiver':'Send PIN / passkey';
  if($('#pairing-form').dataset.prompt!==(p?.id||'')){$('#pairing-input').value='';$('#pairing-form').dataset.prompt=p?.id||'';}
  renderDevices();
}
$('#bluetooth-controller').addEventListener('change',renderBluetooth);
$('#bluetooth-power').addEventListener('click',e=>action(e.target,async()=>{const id=$('#bluetooth-controller').value;const radio=state.bluetooth.controllers.find(c=>c.id===id);state.bluetooth=await api('/bluetooth/power',{controller:id,powered:!radio.powered});renderBluetooth();}));
$('#pairing-form').addEventListener('submit',async e=>{e.preventDefault();const p=state.bluetooth?.pairing.prompt;if(!p)return;try{await api('/bluetooth/answer',{promptId:p.id,value:p.kind==='confirm'?true:$('#pairing-input').value});$('#pairing-input').value='';await refresh();}catch(error){toast(error.message,true);}});
$('#pair-cancel').addEventListener('click',async()=>{try{await api('/bluetooth/cancel',{});await refresh();}catch(error){toast(error.message,true);}});
function renderHistory() {
  const recent=state.scans.filter(scan=>!scan.demo)
    .sort((a,b)=>b.createdAt.localeCompare(a.createdAt) || b.id.localeCompare(a.id)).slice(0,5);
  const list=$('#scan-list');
  const signature=JSON.stringify([recent.map(s=>[s.id,s.vehicle,s.vin,s.createdAt,s.codes.length]),state.selected?.id]);
  if(list.dataset.signature===signature)return;
  list.dataset.signature=signature;
  list.innerHTML=recent.map(s=>{
    const vehicle=s.vehicle, codeCount=s.codes.length;
    return `<button type="button" class="scan-row ${s.id===state.selected?.id?'selected':''}" data-scan="${E(s.id)}">
      <strong>${E(vehicle.make || 'Unknown make')} ${E(vehicle.model || '')}</strong>
      <span class="muted small">${E([vehicle.year,vehicle.trim,vehicle.drivetrain].filter(Boolean).join(' · '))}</span>
      <span class="vehicle-vin">${s.vin?'VIN · '+E(s.vin):'VIN not captured'}</span>
      <span class="row-meta"><time datetime="${E(s.createdAt)}">${E(fleetDate(s.createdAt))}</time>
        <span class="badge ${codeCount?'amber':'neutral'}">${codeCount} CODE${codeCount===1?'':'S'}</span></span></button>`;
  }).join('') || '<div class="empty"><p>No saved scans yet.</p></div>';
}
function fillVehicle(vehicle,vin='') {
  for(const key of ['make','model','year','trim','drivetrain','engine','platform','symptoms'])$('#'+key).value=vehicle[key]??'';
  $('#vin').value=vin||'';
  delete $('#vehicle-form').dataset.dirty;
}
async function selectScan(id, changeView=true) {
  state.selected=await api('/scans/'+id);
  if(!state.selected.demo && (changeView || !$('#vehicle-form').dataset.dirty))fillVehicle(state.selected.vehicle,state.selected.vin);
  renderHistory();renderScan();if(changeView)view('workspace');
}
$('#scan-list').addEventListener('click',e=>{const b=e.target.closest('[data-scan]');if(b)selectScan(b.dataset.scan).catch(e=>toast(e.message,true));});

const emptyScanDetail = $('#scan-detail').innerHTML;
async function deleteScan(id, createdAt) {
  if(!window.confirm(`Delete the scan from ${fleetDate(createdAt)}? Its saved readings and diagnostic file will be permanently deleted.`))return false;
  const result=await deleteRecord('/scans/'+encodeURIComponent(id));
  if(state.selected?.id===id){state.selected=null;renderScan();}
  toast(result.warning || 'Scan deleted.',Boolean(result.warning));
  return true;
}

function renderScan() {
  const s=state.selected;
  if(!s){$('#scan-detail').innerHTML=emptyScanDetail;return;}
  const active=state.status?.adapter.scanId===s.id;
  const v=s.vehicle || {}, r=s.research || {status:'idle'};
  const group=state.vehicles.find(g=>g.scans.some(scan=>scan.id===s.id));
  const sessions=group?.scans || [s];
  $('#scan-detail').dataset.sessions=sessionsForScan(s.id);
  const matched=s.definitionMatches||[];
  const codes=s.codes.map(c=>{
    const refs=matched.filter(d=>d.code===c.code && (!d.ecu || d.ecu.toLowerCase()===(c.ecu || '').toLowerCase()));
    return `<article class="code"><div class="code-head"><strong>${E(c.code)}</strong><span class="badge ${c.status==='pending'?'neutral':'amber'}">${E(c.status||'status unknown')}</span><span class="muted small">${E(c.ecu||'ECU not supplied')}</span></div>${c.definition?`<p>${E(c.definition)}</p><p class="small">Description supplied by scan source; verify against OEM documentation.</p>`:refs.length?'':'<p>Definition not established for this vehicle. Research or an exact OEM reference is needed.</p>'}${refs.map(d=>`<p class="reference">${E(d.origin)} · ${E(d.description)} · ${definitionSource(d)}</p>`).join('')}</article>`;
  }).join('');
  const reportStatus=({idle:'No research run yet.',running:'Searching sources and comparing possible causes…',error:r.error,demo:'Demonstration · no AI request sent'})[r.status] || (r.searched?'Internet sources consulted.':'No web search recorded; verify report sources.');
  const citations=[...new Map((r.citations||[]).filter(c=>safeURL(c.url)!=='#').map(c=>[c.url,c])).values()];
  $('#scan-detail').innerHTML=`
    <div class="detail-title"><div><p class="eyebrow">${s.demo?'DEMONSTRATION':'DIAGNOSTIC SESSION'}</p>
      <h2>${E(v.make)} ${E(v.model)}</h2><p>${E([v.year,v.trim,v.drivetrain,v.engine].filter(Boolean).join(' · '))}<br>${E(date(s.createdAt))}</p>
    </div><div class="actions">${!s.demo?'<button class="button secondary" id="edit-vehicle">Edit vehicle</button>':''}<a class="button secondary" href="/api/scans/${E(s.id)}/summary.pdf">Download PDF</a><button class="button secondary" id="export">Export JSON</button>${state.user?.role==='admin'?'<button type="button" class="button secondary danger" id="delete-scan">Delete scan</button>':''}</div></div>
    <p class="muted small">${E(s.scope||'Coverage not supplied by source.')}</p>
    <p class="vehicle-vin">${s.vin?'VIN · '+E(s.vin)+(s.vinSource==='manual'?' · manually entered':''):'VIN not captured'}</p>
    ${s.scannerId?`<p class="muted small"><a href="#scanners/${E(s.scannerId)}">${E(s.scannerIdentifier || 'Scanner profile')}</a>
      ${s.vehicleId?` · <a href="#dossiers/${E(s.vehicleId)}">Vehicle dossier</a>`:''}${s.scanStatus?' · '+E(s.scanStatus):''}</p>`:''}
    ${s.diagnosticFile && !s.diagnosticFileError?`<p><a href="/api/scans/${E(s.id)}/diagnostic">Download dated diagnostic file</a></p>`:''}
    ${sessions.length>1?`<label class="session-picker">Scan session<select id="scan-session">${sessions.map(scan=>`<option value="${E(scan.id)}" ${scan.id===s.id?'selected':''}>${E(date(scan.createdAt))} · ${scan.codes.length} code${scan.codes.length===1?'':'s'} · ${E(scan.id.slice(0,8))}</option>`).join('')}</select></label>`:''}
    ${(s.warnings||[]).map(w=>`<p class="warning">${E(w)}</p>`).join('')}
    ${(s.monitors||[]).map(m=>`<p class="muted small">ECU ${E(m.ecu)} · MIL ${m.mil?'on':'off'} · Reported DTC count ${E(m.dtcCount)} · Readiness bytes ${E(m.readinessBytes)}</p>`).join('')}
    <div class="readings" id="detail-readings">${readingsHTML(active&&state.status.adapter.latest.length?state.status.adapter.latest:s.readings)}</div>
    <div class="codes"><div class="panel-heading"><h2>Fault codes</h2><span class="badge ${s.codes.length?'amber':'neutral'}">${s.codes.length} RETURNED</span></div>${codes||'<p class="muted small">No codes returned in the responses received. Review warnings and module coverage before concluding the vehicle is fault-free.</p>'}</div>
    <div class="divider"></div><div class="panel-heading"><h2>AI investigation</h2><button class="button secondary" id="research" ${s.demo||!s.codes.length||r.status==='running'?'disabled':''}>${r.status==='complete'?'Research again':'Research codes'}</button></div>
    <p class="report-status ${r.status==='error'?'error-text':''}">${E(reportStatus)}</p>${r.text?`<div class="report">${reportHTML(r)}</div>`:''}
    ${citations.length?`<div class="sources">${citations.map((c,i)=>`<a href="${E(safeURL(c.url))}" target="_blank" rel="noopener noreferrer">[${i+1}] ${E(c.title)}</a>`).join('')}</div>`:''}
    <details><summary>Raw diagnostic evidence</summary><pre>${E(JSON.stringify({protocol:s.protocol,supported:s.supported,freezeFrame:s.freezeFrame,trace:s.trace},null,2))}</pre></details>`;
  $('#scan-session')?.addEventListener('change',e=>selectScan(e.target.value,false).catch(e=>toast(e.message,true)));
  $('#edit-vehicle')?.addEventListener('click',()=>{fillVehicle(v,s.vin);view('connection');$('#model').focus();});
  $('#delete-scan')?.addEventListener('click',event=>action(event.currentTarget,()=>deleteScan(s.id,s.createdAt)));
  $('#export').addEventListener('click',async()=>{try{const data=await api('/scans/'+s.id+'/export');const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=`proactive-${v.make||'scan'}-${s.id.slice(0,8)}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){toast(e.message,true);}});
  $('#research').addEventListener('click',e=>action(e.target,async()=>{await api('/scans/'+s.id+'/research',{});await selectScan(s.id,false);toast('Research started. The report will appear here.');}));
}

async function refresh() {
  if(refreshRunning || !state.authenticated)return;refreshRunning=true;
  try {
    const [status,vehicles,bluetooth,fleet,events]=await Promise.all([api('/status'),api('/vehicles'),api('/bluetooth'),api('/fleet'),api('/fleet/events?limit=100'+(state.fleetCursor==null?'':'&after='+state.fleetCursor))]);
    const scans=vehicles.flatMap(v=>v.scans);
    state.status=status;state.scans=scans;state.vehicles=vehicles;state.bluetooth=bluetooth;
    state.fleet=fleet;state.fleetEvents=[...state.fleetEvents,...events.events].slice(-200);state.fleetCursor=events.nextCursor;
    if(bluetooth.available)state.devices=[...bluetooth.devices,...state.devices.filter(d=>d.transport==='serial')];
    renderStatus();renderHistory();renderBluetooth();renderDevices();renderFleet();
    if($('#adapter-trace-panel').open)$('#adapter-trace').textContent=JSON.stringify(await api('/adapter/trace'),null,2);
    if(state.selected) {
      const entry=scans.find(s=>s.id===state.selected.id);
      if(entry && (JSON.stringify(entry.research)!==JSON.stringify(state.selected.research) || JSON.stringify(entry.vehicle)!==JSON.stringify(state.selected.vehicle) || sessionsForScan(entry.id)!==$('#scan-detail').dataset.sessions)) await selectScan(entry.id,false);
      const a=status.adapter;
      if(a.scanId===state.selected.id && a.latest.length && $('#detail-readings'))$('#detail-readings').innerHTML=readingsHTML(a.latest);
    }
  } catch(e) {$('#connection-badge').textContent='Server unavailable';}
  finally{refreshRunning=false;}
}
$('#discover').addEventListener('click',e=>action(e.target,async()=>{
  $('#discovery-note').textContent='Scanning nearby Bluetooth LE devices for 6 seconds…';
  const result=await api('/discover',{controller:state.bluetooth?.available?$('#bluetooth-controller').value:undefined});state.devices=result.devices;
  $('#discovery-note').textContent=result.warning || `${result.devices.length} receiver(s) found. Match the name and address to your own adapter. Pair if required; many BLE receivers can connect directly.`;
  renderDevices();
}));
$('#device-list').addEventListener('click',e=>{const b=e.target.closest('[data-device],[data-pair],[data-fleet]');if(!b)return;action(b,async()=>{
  const d=state.devices[Number(b.dataset.device??b.dataset.pair??b.dataset.fleet)];
  if(b.hasAttribute('data-fleet')){await api('/fleet/readers',{address:d.id,controller:state.bluetooth.controller,name:d.name,label:d.name});toast('Reader added. The Pi will connect, scan and save dated files automatically.');view('fleet');return;}
  if(b.hasAttribute('data-pair')){await api('/bluetooth/pair',{id:d.id});toast('Pairing started. Complete any PIN or confirmation below.');}
  else {await api('/connect',{...d,baud:Number($('#baud').value)});toast('Receiver connected. Code scanning and dated diagnostic files are automatic.');}
});});
$('#initialize').addEventListener('click',e=>action(e.target,async()=>{await api('/initialize',{tx:Number($('#tx').value),rx:Number($('#rx').value)});toast('Standard OBD capabilities discovered. Ready to scan.');}));
$('#disconnect').addEventListener('click',e=>action(e.target,async()=>{await api('/disconnect',{});toast('Adapter disconnected.');}));
$('#adapter-trace-panel').addEventListener('toggle',async()=>{if($('#adapter-trace-panel').open)try{$('#adapter-trace').textContent=JSON.stringify(await api('/adapter/trace'),null,2);}catch(e){toast(e.message,true);}});
function sessionsForScan(id){return (state.vehicles.find(g=>g.scans.some(s=>s.id===id))?.scans || []).map(s=>s.id).join(',');}
const getVehicle=()=>({make:$('#make').value.trim(),model:$('#model').value.trim(),year:Number($('#year').value),trim:$('#trim').value.trim(),drivetrain:$('#drivetrain').value.trim(),vin:$('#vin').value.trim().toUpperCase(),engine:$('#engine').value.trim(),platform:$('#platform').value.trim(),symptoms:$('#symptoms').value.trim()});
$('#vehicle-form').addEventListener('input',()=>$('#vehicle-form').dataset.dirty='true');
$('#save-vehicle').addEventListener('click',e=>{
  if(!$('#vehicle-form').reportValidity())return;
  action(e.target,async()=>{
    const vehicle=getVehicle();if(!vehicle.vin)throw new Error('Select a saved vehicle or enter its captured VIN first.');
    await api('/vehicles/'+encodeURIComponent(vehicle.vin),{vehicle});
    localStorage.setItem('apex-vehicle',JSON.stringify(vehicle));delete $('#vehicle-form').dataset.dirty;
    if(state.selected?.vin===vehicle.vin)await selectScan(state.selected.id,false);
    toast('Vehicle details saved for this VIN and its existing scans.');
  });
});
$('#vehicle-form').addEventListener('submit',e=>{e.preventDefault();action($('#scan-button'),async()=>{
  localStorage.setItem('apex-vehicle',JSON.stringify(getVehicle()));
  toast('Reading codes, monitor status and freeze-frame evidence…');
  const scan=await api('/scan',{vehicle:getVehicle()});state.selected=scan;fillVehicle(scan.vehicle,scan.vin);localStorage.setItem('apex-vehicle',JSON.stringify({...scan.vehicle,vin:scan.vin}));renderScan();view('workspace');toast('Scan and vehicle VIN saved. Review returned codes and coverage warnings.');
});});
$('#start-poll').addEventListener('click',e=>action(e.target,async()=>{await api('/poll',{enabled:true,pids:$$('input[name="pid"]:checked').map(i=>i.value),interval:Number($('#interval').value)});toast('Live polling started.');}));
$('#stop-poll').addEventListener('click',e=>action(e.target,async()=>{await api('/poll',{enabled:false});toast('Live polling stopped.');}));

function pickFile(selector){const input=$(selector);input.value='';input.click();}
$('#import-scan').addEventListener('click',()=>pickFile('#file-input'));
$('#import-definitions').addEventListener('click',()=>pickFile('#definition-file'));
$('#file-input').addEventListener('change',async e=>{
  const file=e.target.files[0];if(!file)return;
  try{
    if(file.size>5_000_000)throw new Error('Upload limit is 5 MB.');
    const data=JSON.parse(await file.text());
    state.selected=await api('/import',data);renderScan();view('workspace');toast('Scan imported.');
    await refresh();
  }catch(e){toast(e.message,true);}
});
$('#definition-file').addEventListener('change',async e=>{
  const file=e.target.files[0];
  if(!file)return;
  const extension=file.name.split('.').pop().toLowerCase();
  const button=$('#import-definitions');
  button.disabled=true;
  try{
    if(extension!=='json' && extension!=='csv')throw new Error('Choose a .json or .csv file.');
    if(file.size>5_000_000)throw new Error('Upload limit is 5 MB.');
    if(!file.size)throw new Error('The definitions file is empty.');
    const result=await api('/definitions',file,extension==='csv'?'text/csv':'application/json');
    await loadDefinitions();
    toast(`${result.count} reference ${result.count===1?'definition':'definitions'} imported.`);
  }catch(error){toast(error.message,true);}
  finally{button.disabled=false;}
});
const definitionSearch = $('#definition-search');
const definitionForm = $('#definition-source-form');
const definitionStatus = $('#definition-extraction-status');
let definitionPage = 0;
let definitionQuery = '';
let definitionRequest = 0;
let reviewPage = 0;
let extracted = [];
let extractionRunning = false;
let stopExtraction = false;
let approvingDefinitions = false;

function definitionSource(record) {
  const label=E(record.reference || 'Source');
  return record.sourceUrl?`<a href="${E(safeURL(record.sourceUrl))}" target="_blank" rel="noopener noreferrer">${label}</a>`:E(record.reference || '');
}

function definitionTitle(record) {
  const vehicle=record.make?`${record.make} ${record.model} · ${record.yearFrom}–${record.yearTo}`:'Generic OBD-II';
  return [record.code, vehicle, record.ecu].filter(Boolean).join(' · ');
}

async function loadDefinitions(page = definitionPage) {
  const request=++definitionRequest;
  const result=await api(`/definitions/catalog?q=${encodeURIComponent(definitionQuery)}&page=${page}`);
  if(request!==definitionRequest)return;
  definitionPage=page;
  $('#definition-count').textContent=`${result.total.toLocaleString()} references`;
  $('#definition-prev').disabled=page===0;
  $('#definition-next').disabled=(page+1)*result.pageSize>=result.total;
  $('#definition-list').innerHTML=result.records.map(record=>`<article class="reference-row"><strong>${E(definitionTitle(record))}</strong><p>${E(record.description)}</p><p>${E(record.origin)} · ${definitionSource(record)}</p></article>`).join('') || '<p class="muted">No matching definitions.</p>';
}
definitionSearch.addEventListener('submit',event=>{
  event.preventDefault();
  definitionQuery=definitionSearch.elements.query.value.trim();
  loadDefinitions(0).catch(error=>toast(error.message,true));
});
$('#definition-prev').addEventListener('click',()=>loadDefinitions(definitionPage-1).catch(error=>toast(error.message,true)));
$('#definition-next').addEventListener('click',()=>loadDefinitions(definitionPage+1).catch(error=>toast(error.message,true)));
document.addEventListener('obd-view',event=>{
  if(event.detail==='library' && state.authenticated)loadDefinitions().catch(error=>toast(error.message,true));
});

function renderDefinitionReview() {
  const pageSize=25;
  const pages=Math.max(1,Math.ceil(extracted.length/pageSize));
  reviewPage=Math.min(reviewPage,pages-1);
  $('#definition-review').hidden=!extracted.length;
  $('#definition-review-prev').disabled=reviewPage===0 || approvingDefinitions;
  $('#definition-review-next').disabled=reviewPage===pages-1 || approvingDefinitions;
  $('#definition-approve').disabled=approvingDefinitions || !extracted.some(record=>record.selected);
  $('#definition-review-list').innerHTML=extracted.slice(reviewPage*pageSize,(reviewPage+1)*pageSize)
    .map((record,index)=>`<div class="reference-row"><label class="check"><input type="checkbox" data-definition-index="${reviewPage*pageSize+index}" ${record.selected?'checked':''} ${approvingDefinitions?'disabled':''}><span><strong>${E(definitionTitle(record))}</strong><br>${E(record.description)}</span></label></div>`).join('');
}

definitionForm.addEventListener('submit',async event=>{
  event.preventDefault();
  if(extractionRunning || approvingDefinitions)return;
  if(extracted.length && !window.confirm('Replace the definitions currently awaiting review?'))return;
  const fields=definitionForm.elements;
  const file=fields.file.files[0];
  const button=definitionForm.querySelector('button[type="submit"], button:not([type])');
  const stop=$('#definition-stop');
  extractionRunning=true;
  button.disabled=true;
  stopExtraction=false;
  try{
    if(file?.size>1_000_000)throw new Error('Source files can be up to 1 MB.');
    if(file && fields.source.value.trim())throw new Error('Choose either a source file or pasted text.');
    definitionStatus.textContent='Preparing source…';
    const source=file?await file.text():fields.source.value;
    const suffix=file?.name.split('.').pop().toLowerCase();
    const format=({tsv:'csv',csv:'csv',json:'json'})[suffix] || 'text';
    const {batches}=await api('/definitions/prepare',{source,format});
    const defaults=Object.fromEntries(['make','model','yearFrom','yearTo'].filter(key=>fields[key].value.trim()).map(key=>[key,fields[key].value.trim()]));
    const sourceUrl=fields.sourceUrl.value.trim();
    if(sourceUrl && safeURL(sourceUrl)==='#')throw new Error('Use an HTTP or HTTPS source URL.');
    const reference=file?.name || 'Pasted OEM source';
    const staged=new Map();
    let skipped=0;
    extracted=[];
    reviewPage=0;
    renderDefinitionReview();
    stop.hidden=false;
    stop.disabled=false;
    for(let index=0;index<batches.length && !stopExtraction;index++){
      definitionStatus.textContent=`Extracting batch ${index+1} of ${batches.length}…`;
      const result=await api('/definitions/extract',{source:batches[index],defaults});
      skipped+=result.skipped;
      for(const record of result.definitions){
        const key=JSON.stringify([record.make,record.model,record.yearFrom,record.yearTo,record.code,record.ecu,record.description]);
        staged.set(key,{...record,sourceUrl,reference,selected:true});
      }
      extracted=[...staged.values()];
    }
    definitionStatus.textContent=`${extracted.length.toLocaleString()} definitions ready to review.${skipped?` ${skipped} incomplete entries skipped.`:''}${stopExtraction?' Extraction stopped.':''}`;
  }catch(error){definitionStatus.textContent=error.message+(extracted.length?' Completed results are available for review.':'');}
  finally{
    extractionRunning=false;
    button.disabled=false;
    stop.hidden=true;
    renderDefinitionReview();
  }
});
$('#definition-stop').addEventListener('click',event=>{stopExtraction=true;event.currentTarget.disabled=true;});
$('#definition-review-prev').addEventListener('click',()=>{reviewPage--;renderDefinitionReview();});
$('#definition-review-next').addEventListener('click',()=>{reviewPage++;renderDefinitionReview();});
$('#definition-review-list').addEventListener('change',event=>{
  const index=event.target.dataset.definitionIndex;
  if(index===undefined)return;
  extracted[index].selected=event.target.checked;
  $('#definition-approve').disabled=!extracted.some(record=>record.selected);
});
$('#definition-approve').addEventListener('click',async()=>{
  if(extractionRunning || approvingDefinitions)return;
  approvingDefinitions=true;
  renderDefinitionReview();
  let saved=0;
  try{
    const selected=extracted.filter(record=>record.selected);
    for(let index=0;index<selected.length;index+=50){
      const batch=selected.slice(index,index+50);
      await api('/definitions/approve',{definitions:batch});
      const completed=new Set(batch);
      extracted=extracted.filter(record=>!completed.has(record));
      saved+=batch.length;
      definitionStatus.textContent=`Added ${saved.toLocaleString()} definitions.`;
    }
    extracted=[];
    await loadDefinitions(0);
  }catch(error){definitionStatus.textContent=`Added ${saved.toLocaleString()} definitions. ${error.message}`;}
  finally{approvingDefinitions=false;renderDefinitionReview();}
});
$('#definition-format').textContent=JSON.stringify({definitions:[{make:'Exact make',model:'Exact model',yearFrom:2025,yearTo:2026,ecu:'Exact ECU name or response ID',code:'OEM_CODE',description:'Definition from your licensed reference',sourceUrl:'https://manufacturer.example/service-reference',reference:'Document ID, revision and page'}]},null,2);
$('#scan-format').textContent=JSON.stringify({vehicle:{make:'McLaren',model:'Exact model',year:2026,engine:'Engine / software',symptoms:'Describe symptoms'},codes:[{code:'RAW_OEM_CODE',ecu:'Module name or address',status:'stored',definition:null}],readings:[],trace:[],warnings:['Document source-tool coverage here'],scope:'Modules covered by the source tool'},null,2);
$('#scan-format-button').addEventListener('click',()=>$('#scan-format').hidden=!$('#scan-format').hidden);
const aiModelSelect = $('#ai-model');

function renderAIModel(current) {
  aiModelSelect.options[0].textContent='Keep current model ('+current+')';
  aiModelSelect.value=current;
  if(!aiModelSelect.value)aiModelSelect.value='';
}

$('#ai-form').addEventListener('input',()=>$('#ai-form').dataset.dirty='true');
$('#ai-form').addEventListener('submit',e=>{
  e.preventDefault();
  action(e.submitter || $('#ai-form button[type="submit"]'),async()=>{
    const config=await api('/settings',{apiKey:$('#api-key').value,model:aiModelSelect.value || undefined,
      autoResearch:$('#auto-research').checked});
    $('#api-key').value='';
    delete $('#ai-form').dataset.dirty;
    renderAIModel(config.model);
    toast('AI settings applied for this server session.');
  });
});
$('#clear-key').addEventListener('click',e=>action(e.target,async()=>{
  const config=await api('/settings',{clearKey:true});
  $('#api-key').value='';
  delete $('#ai-form').dataset.dirty;
  renderAIModel(config.model);
  toast('AI key cleared from server memory.');
}));

const smtpForm = $('#smtp-form');
const notificationForm = $('#notification-form');
let notificationRules = [];
let smtpLoaded = false;
let notificationsLoading = false;

const storageForm = $('#storage-form');
const storageStatus = $('#storage-status');
const storageMove = $('#move-storage');

function waitForStorageRestart() {
  setTimeout(async()=>{
    try{
      const session=await api('/session');
      if(!session.authenticated){location.reload();return;}
    }catch{}
    waitForStorageRestart();
  },2500);
}

async function loadStorage() {
  const storage=await api('/storage');
  if(storage.available===false)return;
  $('#pi-storage').hidden=false;
  $('#storage-database').textContent=storage.database;
  $('#storage-media').textContent=storage.media;
  const select=storageForm.elements.drive;
  select.replaceChildren(new Option('Select a destination',''),...storage.drives.map(drive=>{
    const option=new Option(drive.label+(drive.id===storage.current?' (current)':drive.writable?'':' (read-only)'),drive.id);
    option.disabled=drive.id===storage.current || !drive.writable;
    return option;
  }));
  const canMove=storage.drives.some(drive=>drive.writable && drive.id!==storage.current);
  select.disabled=storage.pending;
  storageMove.disabled=storage.pending || !canMove;
  storageStatus.textContent=storage.error || (storage.pending?'Moving storage. ProActive will restart.':
    canMove?'':'No other writable storage drive is mounted.');
}

$('#refresh-storage').addEventListener('click',()=>loadStorage().catch(error=>{storageStatus.textContent=error.message;}));
storageForm.addEventListener('submit',async event=>{
  event.preventDefault();
  const select=storageForm.elements.drive;
  if(!window.confirm('Move ProActive storage to '+select.selectedOptions[0].textContent+' and restart?'))return;
  storageMove.disabled=true;
  $('#refresh-storage').disabled=true;
  try{
    await api('/storage',{drive:select.value});
    select.disabled=true;
    storageStatus.textContent='Moving storage. This page will reload when ProActive is back.';
    waitForStorageRestart();
  }catch(error){
    storageMove.disabled=false;
    $('#refresh-storage').disabled=false;
    storageStatus.textContent=error.message;
  }
});

function renderSMTP(config) {
  for(const name of ['host','port','security','sender','username'])smtpForm.elements[name].value=config[name];
  smtpForm.elements.password.value='';
  $('#smtp-password-status').textContent=config.hasPassword?'Leave the password blank to keep the saved password for this SMTP host and username.':'';
  smtpLoaded=true;
}

smtpForm.addEventListener('submit',event=>{
  event.preventDefault();
  const config=Object.fromEntries(new FormData(smtpForm));
  config.port=Number(config.port);
  action(smtpForm.querySelector('button'),async()=>{
    renderSMTP(await api('/smtp',config));
    toast('Outbound email settings saved.');
  });
});
$('#smtp-test-form').addEventListener('submit',event=>{
  event.preventDefault();
  const form=event.currentTarget;
  action(form.querySelector('button'),async()=>{
    await api('/smtp/test',Object.fromEntries(new FormData(form)));
    toast('The mail server accepted the test email.');
  });
});

function setRuleEditor(rule) {
  notificationForm.reset();
  notificationForm.dataset.ruleId=rule?.id || '';
  $('#notification-form-title').textContent=rule?'Edit alert rule':'New alert rule';
  $('#notification-cancel').hidden=!rule;
  if(!rule)return;
  notificationForm.elements.name.value=rule.name;
  notificationForm.elements.codes.value=rule.codes.join(', ');
  notificationForm.elements.recipients.value=rule.recipients.join(', ');
  notificationForm.elements.enabled.checked=rule.enabled;
  notificationForm.elements.includeAI.checked=rule.includeAI;
  notificationForm.elements.name.focus();
}

async function loadNotifications() {
  if(notificationsLoading)return;
  notificationsLoading=true;
  try{
    const {rules,deliveries}=await api('/notifications');
    const ruleList=$('#notification-rules');
    if(JSON.stringify(rules)!==ruleList.dataset.signature){
      ruleList.dataset.signature=JSON.stringify(rules);
      notificationRules=rules;
      ruleList.innerHTML=rules.map(rule=>`<article class="reference-row">
        <div class="panel-heading"><strong>${E(rule.name)}</strong><span class="badge neutral">${rule.enabled?'Enabled':'Paused'}</span></div>
        <p>${rule.codes.map(E).join(' + ')}</p>
        <p>${rule.recipients.map(E).join(', ')}</p><p>AI suggestions ${rule.includeAI?'on':'off'}</p>
        <div class="actions"><button class="text-button" data-edit-rule="${E(rule.id)}">Edit</button>
        <button class="text-button" data-toggle-rule="${E(rule.id)}">${rule.enabled?'Pause':'Enable'}</button>
        <button class="text-button" data-remove-rule="${E(rule.id)}">Remove</button></div>
      </article>`).join('') || '<p class="muted">No alert rules yet.</p>';
    }
    const activity=$('#notification-deliveries');
    if(JSON.stringify(deliveries)!==activity.dataset.signature){
      activity.dataset.signature=JSON.stringify(deliveries);
      activity.innerHTML=deliveries.map(item=>`<article class="reference-row"><strong>${E(item.name)}</strong>
        <p>${E(fleetDate(item.createdAt))} · ${({queued:item.attempts?'Retry scheduled':'Queued',sent:'Accepted by mail server',failed:'Delivery failed'})[item.status]}</p>
        ${item.error?`<p class="error-text">${E(item.error)}</p>`:''}</article>`).join('') || '<p class="muted">No email alerts queued yet.</p>';
    }
  }finally{notificationsLoading=false;}
}

notificationForm.addEventListener('submit',event=>{
  event.preventDefault();
  const fields=notificationForm.elements;
  const splitList=value=>value.split(/[,\n]+/).map(part=>part.trim()).filter(Boolean);
  const rule={id:notificationForm.dataset.ruleId,name:fields.name.value,codes:splitList(fields.codes.value),
    recipients:splitList(fields.recipients.value),enabled:fields.enabled.checked,includeAI:fields.includeAI.checked};
  action(notificationForm.querySelector('button'),async()=>{
    await api('/notifications',rule);
    setRuleEditor();
    await loadNotifications();
    toast('Notification rule saved.');
  });
});
$('#notification-cancel').addEventListener('click',()=>setRuleEditor());
$('#notification-rules').addEventListener('click',event=>{
  const button=event.target.closest('button');
  if(!button)return;
  const {editRule,toggleRule,removeRule}=button.dataset;
  const rule=notificationRules.find(item=>item.id===(editRule||toggleRule||removeRule));
  if(!rule)return;
  if(editRule){setRuleEditor(rule);return;}
  if(removeRule && !window.confirm(`Remove the rule "${rule.name}"?`))return;
  action(button,async()=>{
    if(removeRule)await api(`/notifications/${rule.id}/remove`,{});
    else await api('/notifications',{...rule,enabled:!rule.enabled});
    if(notificationForm.dataset.ruleId===rule.id)setRuleEditor();
    await loadNotifications();
  });
});
document.addEventListener('obd-view',event=>{
  if(!state.authenticated || state.user?.role==='user')return;
  if(event.detail==='notifications')loadNotifications().catch(error=>toast(error.message,true));
  if(event.detail==='setup') {
    if(!smtpLoaded)api('/smtp').then(renderSMTP).catch(error=>toast(error.message,true));
    if(state.user?.role==='admin')loadStorage().catch(error=>toast(error.message,true));
  }
});
setInterval(()=>{
  if(state.authenticated && state.view==='notifications')loadNotifications().catch(()=>{});
},10000);

try{const v=JSON.parse(localStorage.getItem('apex-vehicle')||'null');if(v)fillVehicle(v,v.vin);}catch{}
view(location.hash.slice(1)||'workspace');
api('/session').then(async session=>{if (!session.authenticated || session.user?.mustChangePassword) { showLogin(Boolean(session.user?.mustChangePassword), session.user?.username || ''); return; } signedIn(session);document.dispatchEvent(new CustomEvent('obd-view',{detail:state.view}));await refresh();const latest=[...state.scans].sort((a,b)=>Number(a.demo)-Number(b.demo)||b.createdAt.localeCompare(a.createdAt))[0];if(latest && !state.selected)await selectScan(latest.id,false);}).catch(e=>toast(e.message,true));
setInterval(refresh,2000);
