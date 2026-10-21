'use strict';
const $=id=>document.getElementById(id);
const FILTERS=['cities','deal_types','real_estate_types','room_types','bedroom_types','currency_id','districts','price_from','price_to','area_from','area_to','urbans','statuses'];
let token=null,jobId=null,lastState=null,rows=[],poller=null,sortKey=null,sortDir=1,rowsHasMore=false;
let activeTab='collection';
const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// R3.1.13: parent-scoped catalog; no extra network required by any search.
let locationCatalog=null;
const georgianMap={'ა':'a','ბ':'b','გ':'g','დ':'d','ე':'e','ვ':'v','ზ':'z','თ':'t','ი':'i','კ':'k','ლ':'l','მ':'m','ნ':'n','ო':'o','პ':'p','ჟ':'zh','რ':'r','ს':'s','ტ':'t','უ':'u','ფ':'f','ქ':'q','ღ':'gh','ყ':'y','შ':'sh','ჩ':'ch','ც':'ts','ძ':'dz','წ':'ts','ჭ':'ch','ხ':'kh','ჯ':'j','ჰ':'h'};
function displayLocationName(name){return String(name||'').replace(/[ა-ჰ]/g,c=>georgianMap[c]||c);}
function fillSelect(el,choices,placeholder){const old=el.value;el.replaceChildren();const empty=new Option(placeholder,'');el.add(empty);for(const item of choices)el.add(new Option(displayLocationName(item.name),String(item.id)));el.value=choices.some(c=>String(c.id)===old)?old:'';}
function selectCity(){const id=$('cities').value;const city=locationCatalog?.find(c=>String(c.id)===id);fillSelect($('districts'),city?.districts||[],'Any district');$('districts').disabled=!city||!city.districts.length;selectDistrict();}
function selectDistrict(){const city=locationCatalog?.find(c=>String(c.id)===$('cities').value);const district=city?.districts.find(d=>String(d.id)===$('districts').value);fillSelect($('urbans'),district?.urbans||[],district?'Any microdistrict':'Select district first');$('urbans').disabled=!district||!district.urbans.length;}
async function initLocations(){try{const r=await fetch('/assets/locations.json',{cache:'no-store'});if(!r.ok)throw Error('catalog not available');locationCatalog=(await r.json()).cities;const select=$('cities');select.replaceChildren();select.add(new Option('Select city',''));for(const city of locationCatalog)select.add(new Option(displayLocationName(city.name),String(city.id)));select.value='1';selectCity();invalidatePreflight();}catch(e){error('Could not load location list: '+e.message);$('estimate-btn').disabled=true;}}
$('cities').addEventListener('change',()=>{const d=$('districts');d.value='';selectCity();invalidatePreflight();});
$('districts').addEventListener('change',()=>{selectDistrict();invalidatePreflight();});
const statusLabels=['Old building','New building','Under construction'];
function updateStatuses(){const selected=[...document.querySelectorAll('#status-picker input:checked')].map(x=>x.value);$('statuses').value=selected.join(',');$('status-caption').textContent=selected.length?selected.map(v=>statusLabels[Number(v)-1]).join(', '):'Any building status';invalidatePreflight();}
document.querySelectorAll('#status-picker input').forEach(x=>x.addEventListener('change',updateStatuses));

document.addEventListener('click',e=>{
  const picker=$('status-picker');
  if(picker.open&&!picker.contains(e.target)){
    picker.open=false;
  }
});
// Numeric-only: prevents e/E, minus, plus, decimals, spaces, and non-numeric paste.
for(const id of ['price_from','price_to','area_from','area_to','page_from','page_to','row_limit','single-id']){
 const input=$(id);if(!input)continue;input.type='text';input.inputMode='numeric';input.setAttribute('pattern','[0-9]*');input.autocomplete='off';
 input.addEventListener('beforeinput',e=>{if(e.data&&!/^[0-9]+$/.test(e.data))e.preventDefault();});
 input.addEventListener('input',()=>{const cleaned=input.value.replace(/[^0-9]/g,'');if(input.value!==cleaned)input.value=cleaned;});
 input.addEventListener('paste',e=>{const t=e.clipboardData?.getData('text')||'';if(!/^[0-9]+$/.test(t)){e.preventDefault();input.setRangeText(t.replace(/[^0-9]/g,''),input.selectionStart,input.selectionEnd,'end');input.dispatchEvent(new Event('input',{bubbles:true}));}});
}
const extraFilterIds=['area_from','area_to','room_types','bedroom_types','districts','urbans'];
function collectionValidation(){
 const missing=[];
 if(!$('cities').value)missing.push('city');
 if(!$('deal_types').value)missing.push('deal type');
 if(!$('real_estate_types').value)missing.push('property type');
 if(!$('price_from').value)missing.push('minimum price');
 if(!$('price_to').value)missing.push('maximum price');
 if($('price_from').value&&$('price_to').value&&Number($('price_from').value)>Number($('price_to').value))missing.push('price minimum must not exceed maximum');
 if($('area_from').value&&$('area_to').value&&Number($('area_from').value)>Number($('area_to').value))missing.push('area minimum must not exceed maximum');
 const count=extraFilterIds.filter(k=>$(k).value.trim()).length;
 if(count<3)missing.push(`${3-count} more additional filter${3-count===1?'':'s'} (area min/max, rooms, bedrooms, district, microdistrict)`);
 return missing;
}
function showCollectionValidation(){const missing=collectionValidation(),n=$('collect-validation');n.classList.toggle('invalid',missing.length>0);n.textContent=missing.length?'To continue, set: '+missing.join('; ')+'.':'Required filters complete. You can check the count.';return missing.length===0;}
for(const id of ['cities','deal_types','real_estate_types','price_from','price_to','area_from','area_to','room_types','bedroom_types','districts','urbans']){$(id).addEventListener('input',showCollectionValidation);$(id).addEventListener('change',showCollectionValidation);}
initLocations().then(showCollectionValidation);

function payload(){
  const filters={};for(const key of FILTERS){const v=$(key).value.trim();if(v)filters[key]=v;}
  return {mode:$('mode').value,filters,pages_mode:$('pages_mode').value,
    page_from:Number($('page_from').value),page_to:Number($('page_to').value),
    listings_mode:$('listings_mode').value,row_limit:Number($('row_limit').value)};
}
function error(text){$('api-error').textContent=text;$('api-error').hidden=false;}
function clearError(){$('api-error').hidden=true;}
async function call(path,body,timeoutMs=45000){
  const controller=new AbortController();
  const timeout=timeoutMs>0?setTimeout(()=>controller.abort(),timeoutMs):null;
  try{
    const opts={signal:controller.signal};
    if(body!==undefined)Object.assign(opts,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const resp=await fetch(path,opts);
    let data;
    try{data=await resp.json();}catch(e){throw new Error(`HTTP ${resp.status}: server returned non-JSON`);}
    if(!resp.ok)throw new Error(`${data.error_code||resp.status}: ${data.detail||data.error||'Request failed'}`);
    return data;
  }catch(e){
    if(e.name==='AbortError')throw new Error('Request timed out. Completion is not confirmed.');
    throw e;
  }finally{if(timeout!==null)clearTimeout(timeout);}
}
function freeze(active){for(const node of $('collect-form').querySelectorAll('input,select'))node.disabled=active;document.querySelectorAll('.choice-btn').forEach(btn=>btn.disabled=active);for(const node of $('single-form').querySelectorAll('input,select'))node.disabled=active;document.querySelectorAll('#status-picker input').forEach(node=>node.disabled=active);$('single-start').disabled=active;$('single-stop').disabled=!active;$('estimate-btn').disabled=active;$('start-btn').disabled=active||!token;$('cancel-btn').disabled=!active;}
function countLabel(v){return Number.isInteger(v)?v.toLocaleString('en-US'):'unknown';}
function showProgress(done,total,context=''){
  $('m-total').textContent=countLabel(total);
  $('m-processed').textContent=countLabel(done);
  const known=Number.isInteger(total)&&total>=0;
  $('progress-track').hidden=!known;
  const pct=known?(total===0?100:Math.min(100,Math.round(100*done/total))):null;
  $('progress-fill').style.width=known?pct+'%':'0%';
  $('progress-track').setAttribute('aria-valuenow',known?String(pct):'0');
  $('progress-label').textContent=known?`Processed ${done} of ${total} (${pct}%). ${context}`:`Processed ${done}; total is not yet known. ${context}`;
}
const phaseNames={agent_snapshot_matching:'Matching',agent_matching:'Matching',agent_details:'Loading listing',owner_search:'Owner search',photo_comparison:'Photo comparison',collecting:'Collecting listings',table_write:'Saving',done:'Completed',partial:'Partial result',pending:'Preparing'};
function setLoading(active){
  $('job-state').classList.toggle('is-loading',active);
  $('preflight-summary').classList.toggle('is-loading',active);
  $('job-state').setAttribute('aria-busy',active?'true':'false');
  $('preflight-summary').setAttribute('aria-busy',active?'true':'false');
}
function setPreflight(data){
  const sourceLink=$('source-search-link');sourceLink.hidden=!data.query_url; if(data.query_url)sourceLink.href=data.query_url;
  const snapshot=data.enumeration_evidence;
  $('processed-label').textContent=data.mode==='AGENT'?'AGENTS PROCESSED':'LISTINGS PROCESSED';
  const count=data.enumerated_agent_total??data.enumerated_owner_total??data.source_total;
  $('preflight-summary').textContent=snapshot?
    `Verified pages contain ${data.mode==='OWNER'?'owner listings':'agent listings'}: ${countLabel(count)}. Nonempty pages: ${(snapshot.listing_pages??snapshot.pages_visited)}. Cards received: ${snapshot.raw_rows}.`+
      (snapshot.excluded_rows?` Excluded after local verification: ${snapshot.excluded_rows}.`:'')+
      (snapshot.agent_role_filter_variances?` Filter differences: ${snapshot.agent_role_filter_variances}.`:''):
    `Page checked ${data.probe_page}. Listings on page: ${data.first_page_raw_count}. `+
      (count!==null?`Total: ${countLabel(count)}.`:'Total is unknown.');
  $('job-state').textContent=data.can_start?'READY':'UNAVAILABLE';
  $('phase').textContent=data.can_start?'Ready to start':'Check incomplete';
  showProgress(0,data.scope_total_expected);
  $('m-examined').textContent=snapshot?.raw_rows??data.first_page_raw_count??0;
  $('m-pages').textContent=(snapshot?.listing_pages??snapshot?.pages_visited)??1;
  $('m-rows').textContent='0';
  $('start-btn').disabled=!data.can_start;
  $('run-info').textContent=(data.blockers||[]).join('; ');
  setLoading(false);
}
function updateScopeHint(){
  const allPages=$('pages_mode').value==='all',allRows=$('listings_mode').value==='all';
  $('page-range-fields').hidden=allPages;$('row-limit-field').hidden=allRows;
  $('page_from').disabled=allPages;$('page_to').disabled=allPages;$('row_limit').disabled=allRows;
  const from=Number($('page_from').value),to=Number($('page_to').value),limit=Number($('row_limit').value);
  $('scope-hint').textContent=allPages&&allRows?'':`Scope: ${allPages?'all pages':`${from}–${to}`}, ${allRows?'all listings':`up to ${limit} listings`}.`;
}
function renderProgress(s){
  lastState=s;
  const finished=['COMPLETED','CANCELLED','FAILED','PARTIAL','INTERRUPTED'].includes(s.status);
  $('job-state').textContent=finished?({COMPLETED:'COMPLETED',CANCELLED:'STOPPED',FAILED:'ERROR',PARTIAL:'PARTIAL',INTERRUPTED:'INTERRUPTED'}[s.status]||s.status):'PROCESSING';
  $('phase').textContent=phaseNames[s.phase]||s.phase;
  $('last-id').textContent=s.last_listing_id||'—';
  const m=s.metrics;
  $('m-rows').textContent=m.rows_collected;
  $('m-examined').textContent=m.source_examined??0;
  showProgress(m.listings_processed??0,s.scope_total_actual??s.scope_total_expected);
  $('m-pages').textContent=(s.agent_snapshot_evidence||s.owner_snapshot_evidence)?((s.agent_snapshot_evidence||s.owner_snapshot_evidence).listing_pages??(s.agent_snapshot_evidence||s.owner_snapshot_evidence).pages_visited):(m.listing_pages??0);
  $('m-owner').textContent=`${m.owner_search_calls} requests, ${m.owner_listing_pages??0} pages`;
  $('m-images').textContent=m.image_comparisons;
  $('m-errors').textContent=m.errors;
  $('preflight-summary').textContent=s.agent_snapshot_evidence?
    `Agents: ${countLabel(s.scope_total_expected)}. Processed: ${m.listings_processed??0}.`:
    `Collected: ${m.rows_collected}. Pages with listings: ${m.listing_pages??0}.`;
  $('run-info').textContent=finished?(s.error||''):
    `Candidates: ${m.owner_candidates_examined||0} · Photo comparisons: ${m.image_comparisons||0}`;
  setLoading(!finished);
  if(finished){
    freeze(false);token=null;$('start-btn').disabled=true;$('cancel-btn').disabled=true;
    clearInterval(poller);poller=null;
  }
}
function renderRows(){const query=$('table-filter').value.toLowerCase(),status=$('table-status').value;const displayed=rows.filter(r=>(!status||r.matching_status===status)&&(!query||[r.listing_id,r.city_name,r.district_name,r.urban_name,r.matching_status,r.original_owner_id].join(' ').toLowerCase().includes(query)));if(sortKey)displayed.sort((a,b)=>{const x=a[sortKey],y=b[sortKey];if(x===null||x===undefined)return 1;if(y===null||y===undefined)return -1;return sortDir*(typeof x==='number'&&typeof y==='number'?x-y:String(x).localeCompare(String(y),'en',{numeric:true}));});$('table-count').textContent=`${displayed.length}/${rows.length} rows`;const body=$('table-body');if(!displayed.length){body.innerHTML='<tr><td colspan="9" class="no-data">No rows for the selected filters.</td></tr>';return;}body.innerHTML=displayed.map(r=>{const st=r.matching_status||'PENDING';const cls=st==='MATCH_CONFIRMED'?'confirmed':st==='POSSIBLE_MATCH'?'possible':'';return `<tr><td><a href="${escapeHTML(r.url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(r.listing_id)} ↗</a><div>${escapeHTML(r.listing_updated_at||'')}</div></td><td>${escapeHTML(r.seller_type||'UNKNOWN')}</td><td>${escapeHTML([r.city_name,r.district_name,r.urban_name].filter(Boolean).join(' / '))}</td><td>${escapeHTML(r.price_usd??'—')}</td><td>${escapeHTML(r.area_m2??'—')} m²</td><td>${escapeHTML(r.rooms??'—')} / ${escapeHTML(r.bedrooms??'—')} beds</td><td>${r.original_owner_url?`<a href="${escapeHTML(r.original_owner_url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(r.original_owner_id)} ↗</a>`:'—'}</td><td>${r.photo_urls&&r.photo_urls.length?`<a href="${escapeHTML(r.photo_urls[0])}" target="_blank" rel="noopener noreferrer">${escapeHTML(r.images_count??r.photo_urls.length)} photos ↗</a>`:'—'}</td><td><span class="status-pill ${cls}">${escapeHTML(st)}</span><details><summary>Evidence</summary><div>${escapeHTML(r.reason||'')}</div>${r.source_filter_warning?`<div>Card differs from the MyHome filters: ${escapeHTML(r.source_filter_warning)}</div>`:''}<div>Photo matches: ${escapeHTML(r.matched_photo_count??0)}</div><div>Method: ${escapeHTML(r.match_method??'—')}</div><div>HIST_PHASH similarity: ${escapeHTML(r.photo_similarity??'—')}</div><div>SIFT inliers: ${escapeHTML((r.match_evidence||[])[0]?.sift_ransac_inliers??'—')}</div><div>SIFT inlier ratio: ${escapeHTML((r.match_evidence||[])[0]?.sift_inlier_ratio??'—')}</div><div>hist corr: ${escapeHTML(r.histogram_similarity??'—')}</div>${r.error?`<div>${escapeHTML(r.error)}</div>`:''}</details></td></tr>`}).join('');}
function renderPreview(){
  const node=$('preview-rows');
  if(!rows.length){node.textContent='Real rows will appear after collection.';return;}
  node.innerHTML=rows.slice(0,8).map(r=>`<div class="preview-item"><a target="_blank" rel="noopener noreferrer" href="${escapeHTML(r.url)}">${escapeHTML(r.listing_id)} ↗</a><span>${escapeHTML(r.price_usd??'—')} USD · ${escapeHTML(r.seller_type)}</span><span>${escapeHTML(r.matching_status)}</span></div>`).join('');
}
async function refresh(){if(!jobId)return;
  try{const [s,r]=await Promise.all([call(`/api/jobs/${jobId}`),call(`/api/jobs/${jobId}/rows?limit=2000`)]);
    const index=new Map(rows.map((row,i)=>[row.listing_id,i]));
    for(const row of r.rows){const i=index.get(row.listing_id);if(i!==undefined)rows[i]=row;else{rows.push(row);index.set(row.listing_id,rows.length-1);}}
    rowsHasMore=r.has_more;$('load-more').hidden=!rowsHasMore;
    renderProgress(s);renderRows();renderPreview();
    $('results-jump').hidden=rows.length===0;
    $('single-results-jump').hidden=rows.length===0;
    if(s.single_direction){
      const m=s.metrics,finished=['COMPLETED','CANCELLED','FAILED','PARTIAL','INTERRUPTED'].includes(s.status);
      $('single-state').textContent=finished?(s.status==='COMPLETED'?'COMPLETED':s.status):'PROCESSING';
      $('single-state').classList.toggle('is-loading',!finished);
      $('single-phase').textContent=phaseNames[s.phase]||s.phase;
      $('single-candidates').textContent=countLabel(s.scope_total_expected);
      $('single-processed').textContent=m.listings_processed??0;
      $('single-matches').textContent=m.matches_confirmed??0;
      $('single-images').textContent=m.image_comparisons??0;
      const f=s.single_filters;
      const auto=f?`ID ${f.listing_id}: ${f.city||f.city_id}, ${f.district||f.district_id}, ${f.rooms} rooms, ${f.bedrooms} beds, ${f.area_sqm} m², $${f.price_usd}. Search: $${f.price_from}…$${f.price_to}, ${f.area_from}…${f.area_to} m². First ${f.source_photo_count} photos against all candidate photos.`:'Reading listing details…';
      $('single-summary').textContent=finished?(s.error||`${auto} Matches: ${m.matches_confirmed??0}.`):auto;
      $('single-stop').disabled=finished;
    }
  }catch(e){error('Could not refresh progress: '+e.message);}
}
async function loadMore(){if(!jobId||!rowsHasMore)return;
  try{const r=await call(`/api/jobs/${jobId}/rows?limit=2000&offset=${rows.length}`);
    rows.push(...r.rows);rowsHasMore=r.has_more;$('load-more').hidden=!rowsHasMore;renderRows();renderPreview();}
  catch(e){error(e.message);}
}
$('load-more').addEventListener('click',loadMore);

$('collect-form').addEventListener('submit',async e=>{
 e.preventDefault();clearError();token=null;$('start-btn').disabled=true;
 if(!showCollectionValidation()){$('collect-validation').scrollIntoView({behavior:'smooth',block:'nearest'});return;}
 const criteria=payload();
 if(criteria.pages_mode==='range'&&(!Number.isInteger(criteria.page_from)||!Number.isInteger(criteria.page_to)||criteria.page_from<1||criteria.page_to<criteria.page_from)){
   error('Invalid page range');return;
 }
 if(criteria.listings_mode==='limit'&&(!Number.isInteger(criteria.row_limit)||criteria.row_limit<1)){
   error('Invalid listing limit');return;
 }
 $('estimate-btn').disabled=true;$('estimate-btn').textContent='CHECKING COUNT…';
 $('job-state').textContent='CHECKING';$('phase').textContent='Counting listings';setLoading(true);
 $('preflight-summary').textContent='Loading listings…';
 try{const result=await call('/api/preflight',criteria,0);token=result.preflight_id;setPreflight(result);}
 catch(e){$('job-state').textContent='CHECK FAILED';$('phase').textContent='Error';setLoading(false);
   $('preflight-summary').textContent='Could not retrieve listings.';error(e.message);}
 finally{$('estimate-btn').disabled=false;$('estimate-btn').textContent='CHECK COUNT →';}
});
$('start-btn').addEventListener('click',async()=>{if(!showCollectionValidation()||!token)return;clearError();try{const result=await call('/api/jobs',{preflight_id:token});jobId=result.job_id;rows=[];$('results-jump').hidden=true;$('single-results-jump').hidden=true;renderRows();renderPreview();freeze(true);$('export-csv').disabled=false;$('export-xlsx').disabled=false;await refresh();poller=setInterval(refresh,1100);}catch(e){error(e.message);}});
$('cancel-btn').addEventListener('click',async()=>{if(jobId)try{await call(`/api/jobs/${jobId}/cancel`,{});$('cancel-btn').disabled=true;}catch(e){error(e.message);}});
function invalidatePreflight(){
  if(!lastState||!['RUNNING','PENDING','CANCELLING'].includes(lastState.status)){
    token=null;$('start-btn').disabled=true;$('source-search-link').hidden=true;
    if($('job-state').textContent==='CHECK COMPLETE'){
      $('job-state').textContent='FILTERS CHANGED';
      $('preflight-summary').textContent='Filters changed. Check the count again.';
    }
  }
  updateScopeHint();
}
for(const id of ['mode',...FILTERS,'pages_mode','page_from','page_to','listings_mode','row_limit']){
  $(id).addEventListener('input',invalidatePreflight);
  $(id).addEventListener('change',invalidatePreflight);
}
updateScopeHint();
$('table-filter').addEventListener('input',renderRows);$('table-status').addEventListener('change',renderRows);
for(const format of ['csv','xlsx'])$(`export-${format}`).addEventListener('click',()=>{if(jobId)location.href=`/api/jobs/${jobId}/export?format=${format}`;});

document.querySelectorAll('[data-sort]').forEach(node=>node.addEventListener('click',()=>{const key=node.dataset.sort;sortDir=sortKey===key?-sortDir:1;sortKey=key;renderRows();}));


function initMultiChoices(hiddenId,gridId){
 const input=$(hiddenId),grid=$(gridId),values=Array.from({length:10},(_,i)=>i+1);
 function draw(){
  const selected=new Set(input.value.split(',').filter(Boolean).map(Number));
  grid.innerHTML='';
  values.forEach(n=>{
   const btn=document.createElement('button');btn.type='button';btn.className='choice-btn';
   btn.textContent=n===10?'10+':String(n);btn.dataset.value=String(n);
   btn.setAttribute('aria-pressed',selected.has(n)?'true':'false');
   btn.addEventListener('click',()=>{
     if(selected.has(n))selected.delete(n);else selected.add(n);
     input.value=[...selected].sort((a,b)=>a-b).join(',');draw();invalidatePreflight();showCollectionValidation();
   });grid.appendChild(btn);
  });
 }
 draw();
}
initMultiChoices('room_types','room-types-choices');
initMultiChoices('bedroom_types','bedroom-types-choices');
function selectTab(which){
 activeTab=which;
 const headings={collection:['01 / COLLECTION','Set your own filters','Collection and origin matching','Find owner listings and compare photographs.'],single:['02 / SEARCH BY ID','Search for copies','Search by listing ID','Find original owner or agent copies by photo comparison.'],pdf:['03 / PDF GENERATOR','Export real listings','Listing PDF generator','Create PDF property sheets from 1-10 MyHome listing IDs.']};
 const headingsForTab=headings[which];
 $('workflow-kicker').textContent=headingsForTab[0];
 $('workflow-subkicker').textContent=headingsForTab[1];
 $('workflow-title').textContent=headingsForTab[2];
 $('workflow-description').textContent=headingsForTab[3];
 $('results').hidden=which==='pdf';
 for(const key of ['collection','single','pdf']){
  const on=which===key;
  $(`tab-${key}`).classList.toggle('active',on);
  $(`tab-${key}`).setAttribute('aria-selected',on?'true':'false');
  $(`${key}-tab-panel`).hidden=!on;
 }
}
$('tab-collection').addEventListener('click',()=>selectTab('collection'));
$('tab-single').addEventListener('click',()=>selectTab('single'));
$('tab-pdf').addEventListener('click',()=>selectTab('pdf'));
$('single-form').addEventListener('submit',async e=>{
 e.preventDefault();clearError();
 $('single-state').textContent='SEARCHING';$('single-state').classList.add('is-loading');
 $('single-summary').textContent='Loading listing…';$('single-start').disabled=true;
 try{
   const result=await call('/api/single/jobs',{listing_id:$('single-id').value.trim(),direction:$('single-direction').value});
   token=null;jobId=result.job_id;rows=[];rowsHasMore=false;$('results-jump').hidden=true;$('single-results-jump').hidden=true;
   renderRows();renderPreview();freeze(true);
   $('export-csv').disabled=false;$('export-xlsx').disabled=false;
   if(poller)clearInterval(poller);await refresh();poller=setInterval(refresh,1100);
 }catch(err){error(err.message);$('single-state').classList.remove('is-loading');$('single-start').disabled=false;$('single-summary').textContent='Search did not start.';}
});
$('single-stop').addEventListener('click',async()=>{if(jobId){try{await call(`/api/jobs/${jobId}/cancel`,{});$('single-stop').disabled=true;}catch(err){error(err.message);}}});
for(const linkId of ['results-jump','single-results-jump']){
 $(linkId).addEventListener('click',e=>{e.preventDefault();$('results').scrollIntoView({behavior:'smooth',block:'start'});});
}


// PDF Generator (third independent workflow; does not mutate collection/search state).
let pdfJobId=null,pdfPoller=null;
function validatePdfIDs(raw){
 const list=raw.split(',').map(s=>s.trim());
 if(list.length<1||list.length>10)return {valid:false,message:'Enter between 1 and 10 IDs.'};
 if(list.some(s=>!/^[1-9][0-9]{5,11}$/.test(s)))return {valid:false,message:'Use listing IDs with 6-12 digits, separated by commas (no trailing comma).'};
 if(new Set(list).size!==list.length)return {valid:false,message:'Duplicate IDs are not allowed.'};
 return {valid:true,ids:list};
}
function validatePdfForm(){
 const raw=$('pdf-ids').value.trim();const result=raw?validatePdfIDs(raw):{valid:false,message:'Enter at least one listing ID.'};
 const busy=$('pdf-state').dataset.busy==='true';
 $('pdf-start').disabled=!result.valid||busy;
 $('pdf-validation').textContent=result.valid?`${result.ids.length} listing(s) selected. PDFs are generated from live MyHome data.`:result.message;
 $('pdf-validation').classList.toggle('invalid',!result.valid);
 return result;
}
$('pdf-ids').addEventListener('input',validatePdfForm);
async function updatePdfJob(){
 if(!pdfJobId)return;
 try{
  const j=await call(`/api/pdf/jobs/${pdfJobId}`);
  $('pdf-total').textContent=j.total;
  $('pdf-processed').textContent=j.processed;
  $('pdf-success').textContent=j.success;
  $('pdf-failed').textContent=j.failed;
  $('pdf-current').textContent=j.current_id||'—';
  $('pdf-state').textContent=j.status;
  const finished=['COMPLETED','PARTIAL','FAILED','CANCELLED'].includes(j.status);
  $('pdf-state').dataset.busy=String(!finished);
  $('pdf-summary').textContent=finished?`Finished. ${j.success} PDF(s) ready, ${j.failed} failed.`:`Processing ${j.processed} of ${j.total}. ${j.current_id?`Current ID: ${j.current_id}`:'Waiting for worker…'}`;
  const node=$('pdf-items');node.replaceChildren();
  for(const item of j.items){
   const row=document.createElement('div');row.className='pdf-result-item';
   const title=document.createElement('strong');title.textContent=item.listing_id;
   const desc=document.createElement('span');desc.textContent=`${item.status}${item.reason?': '+item.reason:''}`;
   row.append(title,desc);node.appendChild(row);
  }
  $('pdf-downloads').hidden=!j.download_ready;
  $('pdf-download-zip').href=`/api/pdf/jobs/${pdfJobId}/download?format=zip`;
  $('pdf-download-combined').href=`/api/pdf/jobs/${pdfJobId}/download?format=pdf`;
  $('pdf-stop').disabled=finished;
  if(finished){if(pdfPoller){clearInterval(pdfPoller);pdfPoller=null;}validatePdfForm();}
 }catch(e){$('pdf-summary').textContent='Unable to refresh PDF progress: '+e.message;if(pdfPoller){clearInterval(pdfPoller);pdfPoller=null;}$('pdf-state').dataset.busy='false';validatePdfForm();}
}
$('pdf-form').addEventListener('submit',async e=>{
 e.preventDefault();const check=validatePdfForm();if(!check.valid)return;
 $('pdf-start').disabled=true;$('pdf-state').textContent='QUEUED';$('pdf-state').dataset.busy='true';
 $('pdf-summary').textContent='Starting PDF generation…';$('pdf-downloads').hidden=true;
 $('pdf-items').replaceChildren();
 try{const j=await call('/api/pdf/jobs',{listing_ids:check.ids},30000);
   pdfJobId=j.job_id;$('pdf-stop').disabled=false;
   await updatePdfJob();if(pdfPoller)clearInterval(pdfPoller);pdfPoller=setInterval(updatePdfJob,1500);
 }catch(err){$('pdf-summary').textContent='Could not start: '+err.message;$('pdf-state').textContent='FAILED';$('pdf-state').dataset.busy='false';validatePdfForm();}
});
$('pdf-stop').addEventListener('click',async()=>{
 if(!pdfJobId)return;
 try{await call(`/api/pdf/jobs/${pdfJobId}/cancel`,{});$('pdf-stop').disabled=true;$('pdf-summary').textContent='Stop requested; waiting for current listing…';}catch(e){$('pdf-summary').textContent='Stop error: '+e.message;}
});
validatePdfForm();
