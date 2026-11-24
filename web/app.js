'use strict';
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const state = { status: null, scans: [], vehicles: [], selected: null, devices: [], definitions: [], busy: false, lastScanSignature: '', view: 'workspace' };
const escapeHTML = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const E = escapeHTML;
const date = (v) => v ? new Date(v).toLocaleString(undefined, {dateStyle:'medium', timeStyle:'short'}) : '';
const safeURL = (v) => {try {const u = new URL(v); return ['http:','https:'].includes(u.protocol) ? u.href : '#';} catch {return '#';}};
let toastTimer, fileMode, refreshRunning = false;

function toast(message, error=false) { const el = $('#toast'); el.textContent = message; el.classList.toggle('error', error); el.hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.hidden=true, error ? 12000 : 5000); }
async function api(path, body) {
  const response = await fetch('/api' + path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Apex-Client':'1'}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}
async function action(button, task) {
  if (state.busy) return;
  state.busy = true;
  const old = button.textContent; button.textContent = 'Working…'; button.disabled = true;
  try { await task(); } catch (e) {toast(e.message, true);}
  finally {state.busy=false; button.textContent=old; button.disabled=false; await refresh();}
}
function view(name) {
  if (!document.getElementById(name)?.classList.contains('view')) name='workspace';
  state.view=name; $$('.view').forEach(el=>el.classList.toggle('active',el.id===name));
  $$('.nav').forEach(el=>el.classList.toggle('active',el.dataset.view===name));
  $('#view-name').textContent = ({workspace:'Overview',connection:'Adapter & vehicle',library:'OEM reference library',adaptations:'Adaptation planner',setup:'Setup'})[name];
  history.replaceState(null,'','#'+name);
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
  const sessions=state.scans.filter(s=>!s.demo).length;
  $('#session-count').textContent=`${sessions} saved scan session${sessions===1?'':'s'}`;
  $('#initialize').disabled=state.busy || !a.connected || a.polling;
  $('#disconnect').disabled=state.busy || !a.connected;
  $('#discover').disabled=state.busy || a.connected;
  $('#scan-button').disabled=state.busy || !a.ready;
  $('#start-poll').disabled=state.busy || !a.ready || !a.scanId || a.polling;
  $('#stop-poll').disabled=state.busy || !a.polling;
  $('#adapter-message').textContent=a.error || (a.ready ? `${(a.identity||'').replace(/[\r\n]+/g,' ')} · Protocol ${a.protocol} · ${Object.keys(a.supported).length} responding ECU(s)` : a.connected?(a.profile || 'Connected. Select endpoints and initialize diagnostics.'):'Waiting for an adapter.');
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
  if(document.activeElement!==$('#ai-model') && !$('#ai-form').dataset.dirty) {$('#ai-model').value=ai.model;$('#auto-research').checked=ai.autoResearch;}
}
function renderHistory() {
  const signature=JSON.stringify([state.vehicles.map(g=>[g.key,g.vehicle,g.scans.map(s=>[s.id,s.codes.length]),g.lastScanAt]),state.selected?.id]);
  if($('#scan-list').dataset.signature===signature)return;
  $('#scan-list').dataset.signature=signature;
  $('#scan-list').innerHTML=state.vehicles.length?state.vehicles.map(g=>{
    const s=g.scans[0],v=g.vehicle;
    const selected=g.scans.some(s=>s.id===state.selected?.id);
    return `<button class="scan-row ${selected?'selected':''}" data-scan="${E(g.latestScanId)}"><strong>${E(v.make || 'Unknown make')} ${E(v.model || '')}</strong><div class="row-meta"><span>${E([v.year,v.trim,v.drivetrain].filter(Boolean).join(' · '))}</span></div><span class="vehicle-vin">${g.vin?'VIN · '+E(g.vin):'VIN not captured'}</span><div class="row-meta"><span>${g.scans.length} scan${g.scans.length===1?'':'s'}</span><span class="badge ${g.demo?'neutral':s.codes.length?'amber':''}">${g.demo?'DEMO':s.codes.length+' CODE'+(s.codes.length===1?'':'S')}</span></div><span class="muted small">Last scan · ${E(date(g.lastScanAt))}</span></button>`;
  }).join(''):'<div class="empty"><p>No vehicles yet.<br>A scan will create a saved vehicle record.</p></div>';
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

function renderScan() {
  const s=state.selected;if(!s)return;
  const active=state.status?.adapter.scanId===s.id;
  const v=s.vehicle || {}, r=s.research || {status:'idle'};
  const group=state.vehicles.find(g=>g.scans.some(scan=>scan.id===s.id));
  const sessions=group?.scans || [s];
  $('#scan-detail').dataset.sessions=sessionsForScan(s.id);
  const matched=s.definitionMatches||[];
  const codes=s.codes.map(c=>{
    const refs=matched.filter(d=>d.code===c.code && d.ecu===c.ecu);
    return `<article class="code"><div class="code-head"><strong>${E(c.code)}</strong><span class="badge ${c.status==='pending'?'neutral':'amber'}">${E(c.status||'status unknown')}</span><span class="muted small">${E(c.ecu||'ECU not supplied')}</span></div><p>${E(c.definition || 'Definition not established for this vehicle. Research or an exact OEM reference is needed.')}</p>${c.definition?'<p class="small">Description supplied by scan source; verify against OEM documentation.</p>':''}${refs.map(d=>`<p class="reference">Library: ${E(d.description)} · <a href="${E(safeURL(d.sourceUrl))}" target="_blank" rel="noopener noreferrer">${E(d.reference)}</a> (user-supplied)</p>`).join('')}</article>`;
  }).join('');
  const reportStatus=({idle:'No research run yet.',running:'Searching sources and comparing possible causes…',error:r.error,demo:'Demonstration · no AI request sent'})[r.status] || (r.searched?'Internet sources consulted.':'No web search recorded; verify report sources.');
  const citations=[...new Map((r.citations||[]).filter(c=>safeURL(c.url)!=='#').map(c=>[c.url,c])).values()];
  $('#scan-detail').innerHTML=`
    <div class="detail-title"><div><p class="eyebrow">${s.demo?'DEMONSTRATION':'DIAGNOSTIC SESSION'}</p>
      <h2>${E(v.make)} ${E(v.model)}</h2><p>${E([v.year,v.trim,v.drivetrain,v.engine].filter(Boolean).join(' · '))}<br>${E(date(s.createdAt))}</p>
    </div><div class="actions">${!s.demo?'<button class="button secondary" id="edit-vehicle">Edit vehicle</button>':''}<button class="button secondary" id="export">Export JSON</button></div></div>
    <p class="muted small">${E(s.scope||'Coverage not supplied by source.')}</p>
    <p class="vehicle-vin">${s.vin?'VIN · '+E(s.vin)+(s.vinSource==='manual'?' · manually entered':''):'VIN not captured'}</p>
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
  $('#export').addEventListener('click',async()=>{try{const data=await api('/scans/'+s.id+'/export');const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=`obdwhiz-${v.make||'scan'}-${s.id.slice(0,8)}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){toast(e.message,true);}});
  $('#research').addEventListener('click',e=>action(e.target,async()=>{await api('/scans/'+s.id+'/research',{});await selectScan(s.id,false);toast('Research started. The report will appear here.');}));
}

async function refresh() {
  if(refreshRunning)return;refreshRunning=true;
  try {
    const [status,vehicles]=await Promise.all([api('/status'),api('/vehicles')]);
    const scans=vehicles.flatMap(v=>v.scans);
    state.status=status;state.scans=scans;state.vehicles=vehicles;renderStatus();renderHistory();
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
  const result=await api('/discover',{});state.devices=result.devices;
  $('#discovery-note').textContent=result.warning || `${result.devices.length} device(s) found. Select your own Carista adapter.`;
  $('#device-list').innerHTML=result.devices.map((d,i)=>`<div class="device"><div><strong>${E(d.name)}</strong><p>${E(d.transport==='ble'?'Bluetooth LE':'Serial')} · ${E(d.id)}${d.rssi?' · '+E(d.rssi)+' dBm':''}</p></div><button class="button secondary" data-device="${i}">Connect</button></div>`).join('')||'<p class="muted">No adapters found. Check Bluetooth is enabled, ignition and range.</p>';
}));
$('#device-list').addEventListener('click',e=>{const b=e.target.closest('[data-device]');if(!b)return;action(b,async()=>{
  const d=state.devices[Number(b.dataset.device)];await api('/connect',{...d,baud:Number($('#baud').value)});toast('Adapter connected. Check endpoints, then initialize.');
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
$('#demo').addEventListener('click',e=>action(e.target,async()=>{state.selected=await api('/demo',{});renderScan();toast('Demonstration loaded. No vehicle or AI connection used.');}));

function pickFile(mode){fileMode=mode;$('#file-input').value='';$('#file-input').click();}
$('#import-scan').addEventListener('click',()=>pickFile('scan'));
$('#import-definitions').addEventListener('click',()=>pickFile('definitions'));
$('#file-input').addEventListener('change',async e=>{
  const file=e.target.files[0];if(!file)return;
  try{
    if(file.size>5_000_000)throw new Error('Upload limit is 5 MB.');
    const data=JSON.parse(await file.text());
    if(fileMode==='scan'){state.selected=await api('/import',data);renderScan();view('workspace');toast('Scan imported.');}
    else{const result=await api('/definitions',data);await loadDefinitions();toast(`${result.count} reference definitions imported.`);}
    await refresh();
  }catch(e){toast(e.message,true);}
});
async function loadDefinitions(){
  state.definitions=await api('/definitions');$('#definition-count').textContent=state.definitions.length+' REFERENCES';
  $('#definition-list').innerHTML=state.definitions.length?state.definitions.map(d=>`<div class="reference-row"><strong>${E(d.make)} ${E(d.model)} · ${E(d.yearFrom)}–${E(d.yearTo)} · ${E(d.ecu)} · ${E(d.code)}</strong><p>${E(d.description)}</p><a href="${E(safeURL(d.sourceUrl))}" target="_blank" rel="noopener noreferrer">${E(d.reference)}</a><p class="small">User-supplied · not independently verified</p></div>`).join(''):'<div class="empty"><p>No manufacturer definitions imported yet.</p></div>';
}
$('#definition-format').textContent=JSON.stringify({definitions:[{make:'Exact make',model:'Exact model',yearFrom:2025,yearTo:2026,ecu:'Exact ECU name or response ID',code:'OEM_CODE',description:'Definition from your licensed reference',sourceUrl:'https://manufacturer.example/service-reference',reference:'Document ID, revision and page'}]},null,2);
$('#scan-format').textContent=JSON.stringify({vehicle:{make:'McLaren',model:'Exact model',year:2026,engine:'Engine / software',symptoms:'Describe symptoms'},codes:[{code:'RAW_OEM_CODE',ecu:'Module name or address',status:'stored',definition:null}],readings:[],trace:[],warnings:['Document source-tool coverage here'],scope:'Modules covered by the source tool'},null,2);
$('#scan-format-button').addEventListener('click',()=>$('#scan-format').hidden=!$('#scan-format').hidden);
$('#ai-form').addEventListener('input',()=>$('#ai-form').dataset.dirty='true');
$('#ai-form').addEventListener('submit',e=>{e.preventDefault();action($('#ai-form button'),async()=>{await api('/settings',{apiKey:$('#api-key').value,model:$('#ai-model').value.trim(),autoResearch:$('#auto-research').checked});$('#api-key').value='';delete $('#ai-form').dataset.dirty;toast('AI settings applied for this server session.');});});
$('#clear-key').addEventListener('click',e=>action(e.target,async()=>{await api('/settings',{clearKey:true});$('#api-key').value='';toast('AI key cleared from server memory.');}));

function tireDiameter(value){const m=/^(\d{3})\/(\d{2})R(\d{2})$/i.exec(value.trim());if(!m)throw new Error('Use a tire size such as 275/40R21.');const [w,a,r]=m.slice(1).map(Number);if(w<100||w>500||a<15||a>95||r<10||r>30)throw new Error('Check the tire dimensions.');return w*a/50+r*25.4;}
$('#tire-form').addEventListener('submit',e=>{e.preventDefault();try{const old=tireDiameter($('#tire-old').value),next=tireDiameter($('#tire-new').value),ratio=next/old,speed=Number($('#tire-speed').value);$('#tire-result').innerHTML=`<div class="tire-metrics"><div class="metric"><span>Current diameter</span><strong>${old.toFixed(1)} mm</strong></div><div class="metric"><span>Proposed diameter</span><strong>${next.toFixed(1)} mm</strong></div><div class="metric"><span>Diameter change</span><strong>${((ratio-1)*100).toFixed(2)}%</strong></div><div class="metric"><span>Estimated actual speed</span><strong>${(speed*ratio).toFixed(1)} km/h</strong></div></div><p class="muted small spaced">Estimate assumes original speed calibration and equal effective rolling diameter. Load, wear and tire construction affect actual results. No coding was sent to the vehicle.</p>`;}catch(e){toast(e.message,true);}});

try{const v=JSON.parse(localStorage.getItem('apex-vehicle')||'null');if(v)fillVehicle(v,v.vin);}catch{}
view(location.hash.slice(1)||'workspace');
refresh().then(async()=>{const latest=[...state.scans].sort((a,b)=>Number(a.demo)-Number(b.demo)||b.createdAt.localeCompare(a.createdAt))[0];if(latest && !state.selected)await selectScan(latest.id,false);}).catch(e=>toast(e.message,true));
loadDefinitions().catch(()=>{});
setInterval(refresh,2000);
