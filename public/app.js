'use strict';
const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
  grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  book:'M5 3h14v18H5z M9 7h6 M9 11h6 M9 15h3 M3 7h4 M3 12h4 M3 17h4',
  wallet:'M3 6h17v14H3z M3 6l14-3v3 M15 11h6v5h-6z M17 13.5h1',
  calc:'M5 3h14v18H5z M8 6h8v4H8z M8 14h1 M12 14h1 M16 14h1 M8 18h1 M12 18h1 M16 18h1',
  users:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M17 4a4 4 0 0 1 0 7 M22 21v-2a4 4 0 0 0-3-3.87',
  send:'m22 2-7 20-4-9-9-4 20-7z M22 2 11 13',
  settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z',
  plus:'M12 5v14 M5 12h14', down:'M12 3v17 M5 13l7 7 7-7', up:'M12 21V4 M5 11l7-7 7 7',
  arrow:'M4 12h16 M14 6l6 6-6 6', download:'M12 3v12 M7 10l5 5 5-5 M4 15v6h16v-6',
  food:'M5 3v7a3 3 0 0 0 6 0V3 M8 3v18 M19 21V3c-4 1-5 7 0 9',
  car:'M3 10h18v9H3z M5 10l2-6h10l2 6 M5 14h2 M17 14h2 M5 19v2 M19 19v2',
  office:'M4 6h16v15H4z M8 6V3h8v3 M4 11h16 M10 11v3h4v-3',
  screen:'M3 4h18v13H3z M8 21h8 M12 17v4',
  hotel:'M3 21V5h18v16 M7 9h2 M15 9h2 M7 13h2 M15 13h2 M10 21v-4h4v4',
  info:'M12 8h.01 M12 11v6 M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20',
  spark:'m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3 3-7z',
  check:'m4 12 5 5L20 6', clock:'M12 8v5l3 2 M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20',
  search:'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M15 15l6 6',
  receipt:'M5 2h14v20l-3-2-4 2-4-2-3 2z M8 7h8 M8 11h8 M8 15h5',
  link:'M9 15l6-6 M8 16l-1 1a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0 M16 8l1-1a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.book}"/></svg>`;
const PAGES = {overview:['团队总览','overview','grid'],ledger:['收支明细','ledger','book'],reimbursements:['报销管理','reimbursements','wallet'],calculator:['资金计算','calculator','calc'],members:['团队成员','members','users'],integrations:['机器人与 AI','integrations','send'],settings:['团队设置','settings','settings']};
const STATUS = {draft:'草稿',pending:'待报销',reimbursed:'已报销',posted:'已入账',voided:'已作废'};
const ROLES = {owner:'团队成员 · 共同管理',finance:'团队成员 · 共同管理',member:'团队成员 · 共同管理'};
const COLORS = ['green','orange','blue','purple'];
const state = {user:null,csrf:'',conf:null,members:[],entries:[],integrations:{},report:null,page:'overview',
  filter:{text:'',kind:'all',currency:'all',payer:'all',status:'all',from:'',to:''},reimburseTab:'pending',selected:new Set(),entryKind:'expense',editing:null,entryKey:null,action:null,setup:false,base:null,loadId:0};
const canManage = () => Boolean(state.user?.active);
const isOwner = canManage;
const cur = code => state.conf?.currencies.find(c => c.code === code) || {code,symbol:code,decimals:2};
const money = (amount, code, withCode = true) => {
  const c = cur(code);
  const num = Number(amount);
  if (!Number.isFinite(num)) return '—';
  return `${esc(c.symbol)}${num.toLocaleString('zh-CN',{minimumFractionDigits:c.decimals,maximumFractionDigits:c.decimals})}${withCode ? ` <small class="money-code">${esc(code)}</small>` : ''}`;
};
const badge = status => `<span class="badge ${esc(status)}">${esc(STATUS[status] || status)}</span>`;
const avatar = (name,id=0) => `<span class="avatar ${COLORS[Math.abs(Number(id)) % COLORS.length]}">${esc([...name][0] || 'T')}</span>`;
const options = (items,value,label,selected) => items.map(item => `<option value="${esc(value(item))}" ${String(value(item))===String(selected)?'selected':''}>${esc(label(item))}</option>`).join('');
const currencyOptions = selected => options(state.conf.currencies,c=>c.code,c=>`${c.code} · ${c.name}`,selected);

async function api(path, method='GET', body, extraHeaders={}) {
  const headers = {...extraHeaders};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && state.csrf) headers['X-CSRF-Token'] = state.csrf;
  const response = await fetch('/api'+path,{method,headers,credentials:'same-origin',body:body===undefined?undefined:JSON.stringify(body)});
  let data;
  try { data = await response.json(); } catch { throw new Error('服务器返回异常，请稍后重试'); }
  if (!response.ok) {
    if (response.status===401 && state.user) {
      state.user=null; $$('dialog[open]').forEach(d=>d.close()); await showAuth(false);
    }
    throw new Error(typeof data.error==='string'?data.error:response.status===422?'输入格式不正确，请检查表单':'操作失败，请稍后重试');
  }
  return data;
}

let toastTimer;
function toast(message,error=false) {
  const element = $('#toast');element.textContent=message;element.classList.toggle('error',error);element.hidden=false;
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>element.hidden=true,error?7000:4000);
}
async function busy(button,fn) {
  if (button.disabled) return;
  button.disabled=true;
  try { return await fn(); } finally { button.disabled=false; }
}
function empty(title='这里还很清爽',description='开始记下团队的第一笔收支吧。',action='') {
  return `<div class="empty">${icon('book')}<h3>${esc(title)}</h3><p>${esc(description)}</p>${action}</div>`;
}
function heading(title,subtitle,eyebrow,actions='') {
  return `<div class="page-heading"><div><span class="eyebrow">${eyebrow}</span><h1>${title}</h1><p>${subtitle}</p></div><div class="heading-actions">${actions}</div></div>`;
}
function newButton(kind='expense',text='记一笔') {return `<button class="btn primary" data-new="${kind}">${icon('plus')}${text}</button>`;}
function baseSelect() {return `<select id="report-base" aria-label="汇总参考币种">${currencyOptions(state.base || state.conf.reference_currency)}</select>`;}
function entryIcon(entry) {
  return `<span class="entry-type-icon ${entry.kind==='income'?'income':''}">${icon(entry.kind==='income'?'down':'receipt')}</span>`;
}
function groupedTotals(entries) {
  const sums={};
  for (const e of entries) sums[e.currency]=(sums[e.currency] || 0)+Number(e.amount);
  return Object.entries(sums).map(([code,amount])=>money(amount,code)).join(' <span class="muted"> / </span> ') || '—';
}
function settlementDisplay(entry, report=state.report) {
  const base=report?.base;
  const rate=report?.fx?.rates?.[entry.currency];
  if (!base || entry.currency===base || !rate) return '';
  const convertedAmount=Number(entry.amount)*Number(rate.rate);
  if (!Number.isFinite(convertedAmount)) return '';
  return `<span class="settlement-line">≈ ${money(convertedAmount,base)} <small>结算参考${rate.date?` · ${esc(rate.date)}`:''}</small></span>`;
}
function table(entries,{checkbox=false}={}) {
  if (!entries.length) return empty('还没有符合条件的账目','调整筛选条件，或记下新的一笔。',newButton());
  return `<div class="table-wrap"><table class="data-table"><thead><tr>${checkbox?'<th class="check-cell"><input type="checkbox" id="select-all" aria-label="选择本页全部账目"></th>':''}<th>用途 / 说明</th><th>成员</th><th>日期</th><th class="money">金额</th><th>状态</th><th>来源</th><th></th></tr></thead><tbody>${entries.map(e=>`<tr data-entry="${e.id}" tabindex="0" aria-label="查看 ${esc(e.description)}">${checkbox?`<td class="check-cell"><input type="checkbox" data-select="${e.id}" ${state.selected.has(e.id)?'checked':''} aria-label="选择 ${esc(e.description)}"></td>`:''}<td><div class="entry-cell">${entryIcon(e)}<div><strong>${esc(e.description)}</strong><span class="subline">${e.kind==='income'?'团队收入':'成员垫付'}${e.receipt?' · 已附凭证':''}</span></div></div></td><td>${esc(e.payer_name)}</td><td>${esc(e.date.slice(5).replace('-',' / '))}<span class="subline">${esc(e.date.slice(0,4))}</span></td><td class="money ${e.kind==='income'?'income':''}"><strong>${e.kind==='income'?'+':'−'} ${money(e.amount,e.currency,false)}</strong><span class="subline">${esc(e.currency)}</span>${settlementDisplay(e)}</td><td>${badge(e.status)}</td><td><span class="row-source">${e.source==='telegram'?'↗ Telegram':'▣ Web'}</span></td><td>↗</td></tr>`).join('')}</tbody></table></div>`;
}
function reportWarning(report=state.report) {
  if (!report) return `<div class="callout warning">${icon('info')}<span>资金计算暂不可用，请刷新重试。原币种收支仍可查看。</span></div>`;
  if (report.fx.warning) return `<div class="callout ${report.complete?'warning':'error'}">${icon('info')}<span>${esc(report.fx.warning)}${report.fx.missing.length?' 缺少币种：'+esc(report.fx.missing.join('、')):''}</span></div>`;
  return '';
}
function aiComposer() {
  return `<section class="ai-input-card"><div class="ai-input-header"><strong>✧ 说一句，帮你记下来</strong><span>支出或收入 · 核对后确认入账</span></div><form id="ai-form" class="ai-composer"><input name="text" placeholder="例如：今天帮团队续费设计工具，花了 49 美元" required maxlength="2000" aria-label="自然语言记账"><button class="btn primary" type="submit">${icon('spark')}AI 识别</button></form><div class="form-error" id="ai-error"></div></section>`;
}
function rateDetails(report=state.report) {
  if (!report) return '';
  const rows=Object.entries(report.fx.rates).filter(([code])=>code!==report.base);
  return `<div class="rates">${rows.map(([code,r])=>`<span class="rate-chip">1 ${esc(code)} ≈ ${Number(r.rate).toLocaleString('zh-CN',{maximumFractionDigits:6})} ${esc(report.base)} · ${esc(r.date)}</span>`).join('')}</div><p class="fx-caption">${rows.length?`${esc(report.fx.source)}${report.fx.stale?' · 缓存已过期，获取失败后沿用':''}。周末、节假日可能返回最近工作日。`:'当前账目无需跨币种换算。'}换算仅供资金规划；实际付款按原币种登记，平台不会自动转账。</p>`;
}
function converted(entry,report=state.report) {
  const r=report?.fx.rates[entry.currency];return r?Number(entry.amount)*Number(r.rate):0;
}
function chart() {
  const r=state.report;
  if (!r?.complete) return empty('等待可用汇率','获取汇率后显示最近 7 天的团队资金流。');
  const days=[];
  for(let i=6;i>=0;i--) {
    const d=new Date(state.conf.today+'T12:00:00Z');d.setUTCDate(d.getUTCDate()-i);
    const date=d.toISOString().slice(0,10);
    const income=state.entries.filter(e=>e.kind==='income'&&e.status==='posted'&&e.date===date).reduce((s,e)=>s+converted(e),0);
    const paid=state.entries.filter(e=>e.status==='reimbursed'&&e.paid_at?.slice(0,10)===date).reduce((s,e)=>s+converted(e),0);
    days.push({date,income,paid});
  }
  const max=Math.max(1,...days.flatMap(d=>[d.income,d.paid]));
  return `<div class="chart-container"><svg class="chart" viewBox="0 0 500 168" role="img" aria-label="最近七天团队收入与报销付款，单位 ${esc(r.base)}">${[0,1,2].map(i=>`<line x1="40" x2="493" y1="${22+i*48}" y2="${22+i*48}" class="gridline"/><text x="0" y="${26+i*48}">${(max*(1-i/2)).toLocaleString('zh-CN',{notation:'compact',maximumFractionDigits:1})}</text>`).join('')}${days.map((d,i)=>{const x=57+i*64,ih=d.income/max*96,ph=d.paid/max*96;return `<rect class="income-bar" x="${x}" y="${122-ih}" width="13" height="${ih}" rx="3"><title>${d.date} 收入 ${d.income.toFixed(cur(r.base).decimals)} ${r.base}</title></rect><rect class="paid-bar" x="${x+18}" y="${122-ph}" width="13" height="${ph}" rx="3"><title>${d.date} 报销付款 ${d.paid.toFixed(cur(r.base).decimals)} ${r.base}</title></rect><text x="${x+15}" y="151" text-anchor="middle">${d.date.slice(5).replace('-','/')}</text>`;}).join('')}</svg></div>`;
}

function renderOverview() {
  const r=state.report,ok=r?.complete,base=r?.base || state.conf.reference_currency;
  const pending=state.entries.filter(e=>e.status==='pending').length;
  const metric=k=>ok?money(r[k],base):'—';
  const memberRows=ok?r.members:[];
  return heading('团队的钱，心里有数。','每一笔垫付都有记录，每一次报销都有着落。','YOUR TEAM, IN BALANCE',`<button class="btn" data-export>${icon('download')}导出账本</button>${newButton()}`)+aiComposer()+
  `<div class="overview-grid"><div class="balance-card"><div class="card-label">团队可用余额 <span class="tiny">· 累计参考换算</span></div><div class="card-amount">${metric('cash')}</div><div class="card-foot"><span>收入 − 已报销付款</span><a href="#calculator">资金怎么算？ ↗</a></div></div><div class="metric-card"><div class="card-heading"><div class="card-label">累计团队收入</div><span class="metric-icon">${icon('down')}</span></div><div class="card-amount">${metric('income')}</div><div class="card-foot"><span>今日入账 ${ok?money(r.today_income,base,false):'待换算'}</span>${canManage()?'<a class="tiny-link" href="#ledger" data-income-link>登记收入 ↗</a>':''}</div></div><div class="metric-card"><div class="card-heading"><div class="card-label">团队待报销</div><span class="metric-icon orange">${icon('wallet')}</span></div><div class="card-amount">${metric('due')}</div><div class="card-foot"><span>${pending} 笔待报销</span><a class="tiny-link" href="#reimbursements">去处理 ↗</a></div></div></div>`+
  reportWarning()+
  (ok?`<div class="callout">${icon('spark')}<span>优先报销已开启：当前可预留 <strong>${money(r.reserve,base)}</strong> 用于报销，${Number(r.shortage)>0?`仍有 <strong>${money(r.shortage,base)}</strong> 缺口。`:`预留后结余 <strong>${money(r.remaining,base)}</strong>。`}</span><a href="#calculator">查看资金计算 →</a></div>`:'')+
  `<div class="section-grid"><section class="panel"><div class="panel-heading"><div><h3>最近 7 天，资金流向</h3><p>团队收入与实际报销付款 · ${esc(base)} 参考换算</p></div><div class="chart-key"><span><i></i>收入</span><span><i class="orange"></i>报销</span></div></div>${chart()}</section><section class="panel"><div class="panel-heading"><div><h3>谁还在等报销</h3><p>按成员汇总 · 全部待报销</p></div><a href="#reimbursements" class="tiny-link">全部 ↗</a></div>${memberRows.length?`<div class="member-list">${memberRows.slice(0,4).map(m=>`<div class="member-row">${avatar(m.name,m.id)}<div class="member-info"><strong>${esc(m.name)}${m.id===state.user.id?' · 我':''}</strong><small>${state.entries.filter(e=>e.payer_id===m.id&&e.status==='pending').length} 笔待报销</small></div><div class="member-amount">${money(m.due,base,false)} <small>${esc(base)}</small></div></div>`).join('')}</div>`:empty(ok?'暂时没有待报销':'保留原币种，等待汇率',ok?'一笔笔记清，一次次报完。':'到收支明细查看原始记录。')}</section></div>`+
  `<section class="panel recent-panel"><div class="panel-heading"><div><h3>最近的几笔</h3><p>团队的每一份投入，都在这里。</p></div><a href="#ledger" class="tiny-link">查看全部收支 ↗</a></div>${table(state.entries.slice(0,5))}<div class="table-footer"><span>共 ${state.entries.length} 笔记录 · 原币种金额</span><span>Web 与 Telegram 共用一本账</span></div></section>`;
}
function filteredEntries() {
  const f=state.filter;
  return state.entries.filter(e=>(!f.text||[e.description,e.note,e.payer_name].join(' ').toLowerCase().includes(f.text.toLowerCase())) &&
    (f.kind==='all'||e.kind===f.kind)&&(f.currency==='all'||e.currency===f.currency)&&(f.payer==='all'||e.payer_id===Number(f.payer))&&
    (f.status==='all'||e.status===f.status)&&(!f.from||e.date>=f.from)&&(!f.to||e.date<=f.to));
}
function ledgerResults() {
  const rows=filteredEntries();
  return table(rows)+`<div class="table-footer"><span>已显示 ${rows.length} / ${state.entries.length} 笔 · 原始金额，未合并不同币种</span><span>点击账目查看详情</span></div>`;
}
function renderLedger() {
  const f=state.filter;
  return heading('收支明细','成员垫付、团队进账，一本账就能看清。','EVERY ENTRY COUNTS',`<button class="btn" data-export>${icon('download')}导出</button>${canManage()?'<button class="btn" data-new="income">＋ 登记收入</button>':''}${newButton()}`)+
    aiComposer()+
    `<section class="panel"><div class="filter-tabs">${[['all','全部记录'],['expense','成员垫付'],['income','团队收入']].map(([value,label])=>`<button data-filter-kind="${value}" class="${f.kind===value?'active':''}">${label}</button>`).join('')}</div><div class="filters"><input id="search-entries" placeholder="搜索用途、成员、备注…" value="${esc(f.text)}" aria-label="搜索账目"><select data-filter="currency" aria-label="筛选币种"><option value="all">全部币种</option>${options(state.conf.currencies,c=>c.code,c=>c.code,f.currency)}</select><select data-filter="payer" aria-label="筛选成员"><option value="all">全部成员</option>${options(state.members,m=>m.id,m=>m.name,f.payer)}</select><select data-filter="status" aria-label="筛选状态"><option value="all">全部状态</option>${options(Object.entries(STATUS),x=>x[0],x=>x[1],f.status)}</select><input type="date" data-filter="from" value="${esc(f.from)}" aria-label="开始日期"><input type="date" data-filter="to" value="${esc(f.to)}" aria-label="结束日期"></div><div id="ledger-results">${ledgerResults()}</div></section>`;
}
function reimbursementRows() {
  const tab=state.reimburseTab;
  return state.entries.filter(e=>e.kind==='expense'&&e.status===tab);
}
function batchToolbar() {
  const n=state.selected.size;
  return `<span>已选 ${n} 笔${n?' · 不同币种会分别列示':''}</span>${state.reimburseTab==='pending'?'<button class="btn small primary" data-batch="pay">登记已付款</button>':''}${state.reimburseTab==='draft'?'<button class="btn small primary" data-batch="submit">记入账本</button>':''}`;
}
function renderReimbursements() {
  const r=state.report,base=r?.base||state.conf.reference_currency;
  const metric=k=>r?.complete?money(r[k],base):'—';
  const rows=reimbursementRows();
  const selectable=['pending','draft'].includes(state.reimburseTab);
  return heading('让垫付，及时有回音。','记一笔 → 待报销 → 登记已付款。每次操作都有记录。','LESS WAITING, MORE DOING',newButton())+
    `<div class="reimbursement-summary"><div class="mini-stat"><small>全部待报销 · 参考换算</small><strong>${metric('due')}</strong></div><div class="mini-stat"><small>当前可安排报销资金</small><strong>${metric('payable')}</strong></div><div class="mini-stat"><small>累计已报销</small><strong>${metric('paid')}</strong></div></div>${reportWarning()}<section class="panel"><div class="filter-tabs">${['pending','reimbursed','draft','voided'].map(s=>`<button data-reimburse-tab="${s}" class="${state.reimburseTab===s?'active':''}">${STATUS[s]}<small>${state.entries.filter(e=>e.kind==='expense'&&e.status===s).length}</small></button>`).join('')}</div><div id="reimbursement-results">${table(rows,{checkbox:selectable})}</div>${rows.length&&selectable?`<div class="batch-toolbar" id="batch-toolbar">${batchToolbar()}</div>`:''}</section><div class="notice">每笔垫付记账后直接进入待报销。每位成员都可登记付款；请在实际转账完成后标记已报销。报销按每笔原币种登记，换算结果用于规划。</div>`;
}
function calcSummary(report,scenario=false) {
  if (!report?.complete) return `<div class="calc-summary"><span class="eyebrow">REIMBURSE FIRST</span><div class="calc-amount">等待汇率</div><p>原币种账目已保留，缺少可靠汇率时暂停合并计算。</p></div>${reportWarning(report)}`;
  const b=report.base;
  return `<div class="calc-summary"><span class="eyebrow">${scenario?'WHAT IF · 新进账预演':'REIMBURSE FIRST · 优先覆盖待报销'}</span><div class="calc-amount">${money(report.reserve,b)}</div><p>建议预留为报销资金 · 覆盖全部待报销款项</p><div class="calculation-equation">可用资金 = 累计收入 − 已报销付款${scenario?' + 本次模拟进账':''}<br>预留报销 = 可用正余额与待报销总额的较小值</div><div class="calc-row"><span>${scenario?'模拟后的':'当前'}可用资金</span><strong>${money(report.cash,b)}</strong></div><div class="calc-row"><span>全部待报销</span><strong>${money(report.due,b)}</strong></div><div class="calc-row"><span>当前可安排付款</span><strong>${money(report.payable,b)}</strong></div><div class="calc-row highlight"><span>预留报销后结余</span><strong>${money(report.remaining,b)}</strong></div><div class="calc-row"><span>还需补充的资金</span><strong>${money(report.shortage,b)}</strong></div></div>`;
}
function planTable(report) {
  if (!report?.complete||!report.members.length) return empty('暂时没有待报销','记下成员垫付后，这里会显示每位成员的报销情况。');
  const entryLabel = entry => {
    const hasSuggestion=Number(entry.suggested)>0;
    const full=entry.fully_covered;
    const status=hasSuggestion?(full?'建议付款':'部分安排'):'暂不安排';
    const original=money(entry.amount,entry.currency,false);
    const converted=entry.currency===report.base?'':` · 结算参考 ${money(entry.settlement,report.base,false)}`;
    return `<div class="plan-entry ${hasSuggestion?'is-selected':'is-waiting'}"><div class="plan-entry-main"><span class="plan-entry-status">${status}</span><strong>${esc(entry.description)}</strong><small>${esc(entry.date)} · 原币 ${original}${converted}</small></div><div class="plan-entry-suggested">${hasSuggestion?`建议 ${money(entry.suggested,report.base,false)}`:'—'}</div></div>`;
  };
  return `<div class="table-wrap"><table class="data-table plan-table"><thead><tr><th>成员 / 具体项目</th><th class="money">全部待报销</th><th class="money">建议安排付款</th></tr></thead><tbody>${report.members.map(m=>`<tr><td><strong>${esc(m.name)}</strong><div class="plan-entry-list">${m.entries.map(entryLabel).join('')}</div></td><td class="money">${money(m.due,report.base,false)}</td><td class="money"><strong>${money(m.suggested,report.base,false)}</strong><small class="subline">按项目明细安排</small></td></tr>`).join('')}</tbody></table></div>`;
}
function renderCalculator() {
  const r=state.report;
  return heading('这笔进账，能报多少？','先把伙伴们的垫付安排好，再看团队还剩多少。','REIMBURSE FIRST, THEN THE REST',baseSelect())+
    `<div class="calculator-grid"><section class="panel"><div class="panel-inner"><h2>新进账，先算一算</h2><p class="calculator-intro">把本次新增收入放进当前账本，预演报销和结余。这里的输入不会自动入账。</p><form id="calculator-form"><div class="form-grid"><label>本次新增进账<input name="amount" inputmode="decimal" placeholder="例如：1000.00" required></label><label>收入币种<select name="currency">${currencyOptions(state.conf.default_currency)}</select></label></div><button class="btn primary wide" type="submit">${icon('calc')}计算能报多少</button><div class="form-error" id="calculator-error"></div></form><div class="notice"><strong>默认规则：优先覆盖全部待报销。</strong><p>资金不足时显示缺口；资金充足时显示预留报销后的结余。全部待报销账目均可安排付款，按最早发生日期优先安排。</p><p>若建议金额小于某笔报销金额，只代表资金规划；系统不支持把同一笔账标记为部分付款。</p></div>${canManage()?'<button class="btn wide" data-new="income">＋ 把真实进账记入账本</button>':''}</div></section><section id="calculator-result">${calcSummary(r)}</section></div><section class="panel recent-panel"><div class="panel-heading"><div><h3>伙伴们的报销安排</h3><p>统一按 ${esc(r?.base||state.conf.reference_currency)} 参考换算 · 实际转账仍按原币种</p></div><a class="tiny-link" href="#reimbursements">处理报销 ↗</a></div><div id="plan-table">${planTable(r)}</div></section><section class="panel recent-panel"><div class="panel-inner"><div class="page-subheading"><h3>本次换算依据</h3><button class="btn small" id="refresh-fx">刷新汇率 ↻</button></div><div id="fx-details">${rateDetails(r)}${reportWarning(r)}</div><p class="fx-caption">当前各币种资金余额：${r?Object.entries(r.balances).map(([c,a])=>`${esc(c)} ${esc(a)}`).join(' · ')||'尚无收入或报销付款':'—'}。不同币种账户之间是否实际换汇，需要团队自行确认。</p></div></section>`;
}
function renderMembers() {
  return heading('一起做事的伙伴。','每人一个账号，垫付归属清晰，协作更轻松。','GOOD TEAMS, CLEAR BOOKS',isOwner()?'<button class="btn primary" id="add-member">＋ 邀请新伙伴</button>':'')+
    `<div class="members-grid">${state.members.map(m=>`<section class="person-card ${m.active?'':'inactive'}"><div class="person-card-top">${avatar(m.name,m.id)}<div><h3>${esc(m.name)}${m.id===state.user.id?' · 我':''}</h3><p class="muted">@${esc(m.username)}</p></div></div><hr><div class="person-detail"><span>账本权限</span><strong>${m.active?'共同管理':'已停用'}</strong></div><div class="person-detail"><span>Telegram</span><strong>${m.telegram_linked?'已绑定':'未绑定'}</strong></div><div class="person-detail"><span>待报销 · 原币种</span><strong>${groupedTotals(state.entries.filter(e=>e.payer_id===m.id&&e.status==='pending'))}</strong></div><div class="person-actions"><button class="btn small" data-reset-password="${m.id}">重置密码</button><button class="btn small" data-toggle-member="${m.id}">${m.active?'停用账号':'启用账号'}</button></div></section>`).join('')}</div><div class="notice">扁平化团队，每位启用的成员拥有相同管理权限：记账、登记收入、报销付款、管理成员和设置。独立账号与操作记录，帮助大家追溯每一笔。</div>`;
}
function renderIntegrations() {
  const i=state.integrations;
  return heading('在哪里，都能好好记账。','Web 上管全局，Telegram 里随手记。','ONE LEDGER, TWO WAYS')+
    `<div class="integration-grid"><section class="panel integration-card"><div class="integration-icon">${icon('send')}</div><div class="integration-title"><h2>Telegram 机器人</h2><span class="badge ${i.telegram_linked?'posted':'draft'}">${i.telegram_linked?'账号已绑定':'账号未绑定'}</span></div><p>按钮一步步记录，或发一句话让 AI 帮你填写。确认后，账目立即同步到 Web。</p><div class="integration-steps"><div class="integration-step"><span class="step-number">1</span><span>团队通过 BotFather 创建机器人，配置服务器的 TELEGRAM_BOT_TOKEN，并启动机器人进程。</span></div><div class="integration-step"><span class="step-number">2</span><span>点击下方生成 10 分钟内有效的一次性绑定码，在机器人私聊中发送 /start 绑定码。</span></div><div class="integration-step"><span class="step-number">3</span><span>发送 /book 按按钮记账；也可以说「今天订阅软件花了 20 美元」，确认预览后入账。</span></div></div><div class="notice">机器人配置：${i.bot_configured?'已配置':'尚未配置'} · 进程：${i.bot_online?'在线':'尚未检测到在线'}${i.bot_username?`<p>机器人：<a href="https://t.me/${esc(i.bot_username)}" target="_blank" rel="noreferrer">@${esc(i.bot_username)} ↗</a></p>`:''}</div><button class="btn primary" id="generate-link">${i.telegram_linked?'重新生成绑定码':'生成绑定码'}</button>${i.telegram_linked?' <button class="btn danger" id="unlink-telegram">解除绑定</button>':''}<div id="link-code-result"></div></section><section class="panel integration-card"><div class="integration-icon ai">${icon('spark')}</div><div class="integration-title"><h2>AI 自然语言记账</h2><span class="badge ${i.ai_configured?'posted':'draft'}">${i.ai_configured?'已配置':'等待配置'}</span></div><p>你说清花了什么、多少金额，AI 把它整理成待确认的账目。支持 Web 和 Telegram。</p><div class="code-block">今天打车去客户公司，花了 38 元\n刚给团队续费了 49 美元的设计工具\n今天收到客户尾款 2,000 美元</div><div class="notice">AI 只提取一笔账目的字段，不自动付款，也不直接写入账本。你确认后，系统会再次检查金额、币种和权限。</div><h3>服务器配置项</h3><div class="code-block">AI_BASE_URL=你的兼容接口地址\nAI_API_KEY=你的接口密钥\nAI_MODEL=接口支持的模型名称</div><p class="fx-caption">密钥仅存放在服务器 .env，不会展示在浏览器。尚未配置时，Web 表单和 Telegram 按钮记账仍可使用。</p><a href="#ledger" class="btn wide">去收支明细试试 →</a></section></div>`;
}
function currencyEditorRow(c) {
  return `<div class="currency-edit-row"><input data-cur="code" value="${esc(c.code)}" maxlength="3" aria-label="币种代码" placeholder="USD" required><input data-cur="name" value="${esc(c.name)}" maxlength="20" aria-label="币种名称" placeholder="美元" required><input data-cur="symbol" value="${esc(c.symbol)}" maxlength="8" aria-label="币种符号" placeholder="$" required><select data-cur="decimals" aria-label="币种小数位">${[0,2,3,4].map(d=>`<option ${d===c.decimals?'selected':''}>${d}</option>`).join('')}</select><button type="button" class="icon-button" data-remove-currency aria-label="移除此币种">×</button></div>`;
}
function renderSettings() {
  const c=state.conf;
  return heading('让账本，适合你们。','设置团队名称、常用币种和机器人可用群组。','MADE FOR YOUR TEAM')+
    `<form id="settings-form"><div class="settings-grid"><section class="panel"><div class="panel-inner"><h3>团队与偏好</h3><label>团队名称<input name="team_name" value="${esc(c.team_name)}" maxlength="40" required></label><label>默认记账币种<select name="default_currency" id="default-currency">${currencyOptions(c.default_currency)}</select></label><label>资金汇总参考币种<select name="reference_currency" id="reference-currency">${currencyOptions(c.reference_currency)}</select></label><p class="fx-caption">当前账目日期按 ${esc(c.timezone)} 计算。时区可在服务器 TEAM_TIMEZONE 中修改。</p></div></section><section class="panel"><div class="panel-inner"><h3>可用币种</h3><p class="fx-caption">可添加三字币种代码。已有账目的币种不能移除或修改小数位。</p><div class="currencies-editor"><div class="currency-edit-head"><span>代码</span><span>名称</span><span>符号</span><span>小数位</span><span></span></div><div id="currency-rows">${c.currencies.map(currencyEditorRow).join('')}</div><button type="button" class="btn small wide" id="add-currency">＋ 添加币种</button></div><label>Telegram 允许使用的群组 ID <span class="optional">一行一个</span><textarea name="telegram_allowed_chats" rows="3" placeholder="例如：-1001234567890">${esc(c.telegram_allowed_chats.join('\n'))}</textarea></label><p class="fx-caption">私聊绑定后即可记账。群组需要额外加入允许名单，每位发起记账的成员仍需绑定个人账号。未在名单中的群组不会收到机器人记账回复。</p></div></section></div><div class="form-error" id="settings-error"></div><div class="setting-save"><button class="btn primary" type="submit">保存团队设置</button></div></form>`;
}
const renderers={overview:renderOverview,ledger:renderLedger,reimbursements:renderReimbursements,calculator:renderCalculator,members:renderMembers,integrations:renderIntegrations,settings:renderSettings};
function render() {
  if(!state.user||!state.conf)return;
  $('#content').innerHTML=renderers[state.page]();
  $('#breadcrumb-title').textContent=PAGES[state.page][0];
  $('#workspace-name').textContent=state.conf.team_name;
  $('#navigation').innerHTML=Object.entries(PAGES).filter(([key])=>key!=='settings').map(([key,[name,,ico]])=>`<a href="#${key}" class="${state.page===key?'active':''}">${icon(ico)}<span>${name}</span>${key==='reimbursements'&&state.entries.some(e=>e.status==='pending')?`<small class="nav-count">${state.entries.filter(e=>e.status==='pending').length}</small>`:''}</a>`).join('');
  $('#settings-link').innerHTML=icon('settings')+'团队设置';
  $('#my-avatar').textContent=[...state.user.name][0];$('#my-name').textContent=state.user.name;$('#my-role').textContent=ROLES[state.user.role];
  $('#top-user').textContent=state.user.name;$('#top-date').textContent=new Date(state.conf.today+'T12:00:00').toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'long'});
}
async function refresh(renderAfter=true) {
  const loadId=++state.loadId;
  const [conf,members,entries,integrations]=await Promise.all([api('/settings'),api('/members'),api('/entries'),api('/integrations')]);
  if(loadId!==state.loadId)return;
  state.conf=conf;state.members=members;state.entries=entries;state.integrations=integrations;
  try {state.report=await api('/report'+(state.base?'?base='+encodeURIComponent(state.base):''));}catch(e){state.report=null;toast(e.message,true);}
  if(loadId!==state.loadId)return;
  if(renderAfter)render();
}
async function showAuth(setup) {
  state.setup=setup;$('#loading').hidden=true;$('#app').hidden=true;$('#auth').hidden=false;
  $('#auth-title').textContent=setup?'给团队，开一本账。':'欢迎回来';
  $('#auth-subtitle').textContent=setup?'创建第一个成员账号，和伙伴们共同管理团队账本。':'登录，继续管理团队的每一笔收支。';
  $('#auth-submit').textContent=setup?'创建团队账本 →':'登录账本 →';
  $$('.setup-only').forEach(e=>{e.hidden=!setup;$('input',e).required=setup;});
  $('#auth-form [name=password]').autocomplete=setup?'new-password':'current-password';
}
async function enterApp(data) {
  state.user=data.user;state.csrf=data.csrf;$('#auth').hidden=true;$('#app').hidden=false;$('#loading').hidden=true;
  state.page=Object.hasOwn(PAGES,location.hash.slice(1))?location.hash.slice(1):'overview';
  $('#content').innerHTML='<div class="skeleton">正在整理团队账本…</div>';
  await refresh();
}
async function start() {
  try {
    const boot=await api('/bootstrap');
    if(boot.needs_setup){await showAuth(true);return;}
    try {await enterApp(await api('/session'));}catch(e){if(state.user)throw e;await showAuth(false);}
  }catch(e){$('#loading').innerHTML=`<p>${esc(e.message)}</p><button class="btn" data-reload>重新连接</button>`;}
}

function openEntry(kind='expense',data=null) {
  const form=$('#entry-form');form.reset();state.editing=data?.id || null;state.entryKey=crypto.randomUUID();
  state.entryKind=data?.kind || kind;
  form.elements.currency.innerHTML=currencyOptions(data?.currency || state.conf.default_currency);
  form.elements.payer_id.innerHTML=options(state.members.filter(m=>m.active),m=>m.id,m=>m.name,data?.payer_id || state.user.id);
  form.elements.payer_id.disabled=!canManage();
  form.elements.date.value=data?.date || state.conf.today;
  form.elements.amount.value=data?.amount || '';form.elements.description.value=data?.description || '';form.elements.note.value=data?.note || '';
  $('#entry-kind').hidden=!!data?.id;
  $$('#entry-kind button').forEach(b=>b.hidden=b.dataset.kind==='income'&&!canManage());
  $('#entry-error').textContent='';$('#upload-label').innerHTML='＋ 添加发票或凭证 <small>PNG / JPG / PDF，最多 6 MB</small>';
  entryKindChanged();$('#entry-dialog').showModal();
}
function entryKindChanged() {
  const income=state.entryKind==='income';
  $$('#entry-kind button').forEach(b=>b.classList.toggle('active',b.dataset.kind===state.entryKind));
  $('#entry-title').textContent=state.editing?'修改账目':income?'登记一笔团队收入':'记一笔成员垫付';
  $('#save-draft').hidden=income;
  $('#payer-label').firstChild.textContent=income?'经手成员':'垫付成员';
  $('#save-entry').textContent=income?'确认收入入账':'确认记账';
  $('#entry-hint').textContent=income?'收入入账后会更新团队可用资金。':'记账后直接加入待报销，付款后标记已报销。';
}
async function saveEntry(status,button) {
  const form=$('#entry-form');if(!form.reportValidity())return;
  await busy(button,async()=>{
    $('#entry-error').textContent='';
    try {
      const file=form.elements.receipt.files[0];
      if(file&&file.size>6*1024*1024)throw new Error('凭证大小不能超过 6 MB');
      const body=Object.fromEntries(new FormData(form).entries());delete body.receipt;
      body.payer_id=Number(form.elements.payer_id.value);body.kind=state.entryKind;body.status=status;
      const entry=await api(state.editing?`/entries/${state.editing}`:'/entries',state.editing?'PUT':'POST',body,{'Idempotency-Key':state.entryKey});
      if(file) {
        try {
          const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(new Error('读取凭证失败'));reader.readAsDataURL(file);});
          await api(`/entries/${entry.id}/receipt`,'POST',{data,name:file.name});
        }catch(e){toast(`账目 #${entry.id} 已保存，但凭证未上传：${e.message}。可在详情页补传。`,true);}
      }
      $('#entry-dialog').close();toast(`已${state.entryKind==='income'?'登记收入':status==='draft'?'保存草稿':'记账'} · #${entry.id}`);await refresh();
    }catch(e){$('#entry-error').textContent=e.message;}
  });
}

async function openDetail(id) {
  const e=state.entries.find(x=>x.id===id);if(!e)return;
  $('#detail-content').innerHTML='<div class="skeleton">正在打开账目…</div>';$('#detail-dialog').showModal();
  try {
    const events=await api(`/entries/${id}/events`);
    const mine=e.payer_id===state.user.id||canManage();
    const actions=[];
    if(mine&&['draft','pending'].includes(e.status))actions.push(`<button class="btn" data-edit-entry="${id}">修改账目</button>`);
    if(mine&&e.status==='draft')actions.push(`<button class="btn primary" data-action="submit" data-id="${id}">记入账本</button>`);
    if(canManage()&&e.status==='pending')actions.push(`<button class="btn primary" data-action="pay" data-id="${id}">登记已付款</button>`);
    if(!['reimbursed','voided'].includes(e.status)&&mine)actions.push(`<button class="btn danger" data-action="void" data-id="${id}">作废账目</button>`);
    const actionNames={created:'创建了账目',submit:'将草稿记入账本',approve:'历史记录：通过',reject:'历史记录：退回',pay:'登记了报销付款',edited:'修改了账目',receipt:'上传了凭证',void:'作废了账目'};
    $('#detail-content').innerHTML=`<div class="modal-heading"><div><span class="eyebrow">ENTRY #${e.id}</span><h2>账目详情</h2></div><button class="icon-button" data-close aria-label="关闭">✕</button></div><div class="detail-hero">${entryIcon(e)}<div><h2>${e.kind==='income'?'+':'−'} ${money(e.amount,e.currency)}</h2>${settlementDisplay(e)}<p>${esc(e.description)}</p></div></div>${badge(e.status)}<dl class="detail-list"><div><dt>${e.kind==='income'?'经手成员':'垫付成员'}</dt><dd>${esc(e.payer_name)}</dd></div><div><dt>发生日期</dt><dd>${esc(e.date)}</dd></div><div><dt>记录来源</dt><dd>${esc(e.source)} · ${esc(e.creator_name)}</dd></div><div class="full-span"><dt>备注</dt><dd>${esc(e.note||'—')}</dd></div>${e.review_note?`<div class="full-span"><dt>处理 / 付款备注</dt><dd>${esc(e.review_note)}</dd></div>`:''}<div class="full-span"><dt>凭证</dt><dd>${e.receipt?`<a class="tiny-link" href="/api/entries/${id}/receipt" target="_blank" rel="noreferrer">下载 ${esc(e.receipt_name)} ↗</a>`:'未附凭证'}</dd></div></dl>${mine&&['draft','pending','posted'].includes(e.status)?`<label class="upload-zone"><input type="file" id="detail-receipt" data-id="${id}" accept="image/png,image/jpeg,application/pdf"><span>＋ ${e.receipt?'替换':'补充'}凭证 <small>PNG / JPG / PDF，最多 6 MB</small></span></label>`:''}<h3>操作记录</h3><div class="timeline">${events.map(event=>`<div class="timeline-item">${esc(event.name)} ${esc(actionNames[event.action]||event.action)}${event.detail?`<br>${esc(event.detail)}`:''}<small>${esc(event.created_at.replace('T',' ').slice(0,19))}</small></div>`).join('')}</div><div class="detail-actions">${actions.join('')}</div>`;
  }catch(e){$('#detail-content').innerHTML=empty('加载失败',e.message);}
}
function openAction(action,ids) {
  if(!ids.length){toast('请先选择要处理的账目',true);return;}
  const rows=state.entries.filter(e=>ids.includes(e.id));
  const labels={submit:'记入账本',pay:'登记已付款',void:'作废账目'};
  state.action={action,ids};$('#action-form').reset();$('#action-title').textContent=labels[action];$('#action-confirm').textContent=`确认${labels[action]}`;
  $('#action-description').textContent=action==='pay'?`请确认这 ${ids.length} 笔报销已经实际转账完成。此操作会更新账本余额，不会发起转账。`:`即将对 ${ids.length} 笔账目执行${labels[action]}。`;
  $('#action-totals').innerHTML=groupedTotals(rows);$('#action-note-label').hidden=action==='submit';
  $('#action-note-label').firstChild.textContent=action==='pay'?'付款方式 / 付款参考号':action==='void'?'作废原因':'备注';
  $('#action-form [name=note]').required=['pay','void'].includes(action);$('#action-error').textContent='';$('#action-dialog').showModal();
}

document.addEventListener('click',async event=>{
  const b=event.target.closest('button,a,tr');if(!b)return;
  if(b.hasAttribute('data-close')){b.closest('dialog').close();return;}
  if(b.hasAttribute('data-reload')){location.reload();return;}
  if(b.hasAttribute('data-new')){event.preventDefault();openEntry(b.dataset.new);return;}
  if(b.hasAttribute('data-export')){event.preventDefault();window.location.href='/api/export';return;}
  if(b.hasAttribute('data-income-link')){event.preventDefault();openEntry('income');return;}
  if(b.hasAttribute('data-kind')){state.entryKind=b.dataset.kind;entryKindChanged();return;}
  if(b.hasAttribute('data-filter-kind')){state.filter.kind=b.dataset.filterKind;render();return;}
  if(b.hasAttribute('data-reimburse-tab')){state.reimburseTab=b.dataset.reimburseTab;state.selected.clear();render();return;}
  if(b.hasAttribute('data-batch')){openAction(b.dataset.batch,[...state.selected]);return;}
  if(b.hasAttribute('data-entry')&&!event.target.closest('input,a,button')){openDetail(Number(b.dataset.entry));return;}
  if(b.hasAttribute('data-edit-entry')){const e=state.entries.find(e=>e.id===Number(b.dataset.editEntry));$('#detail-dialog').close();openEntry(e.kind,e);return;}
  if(b.hasAttribute('data-action')){openAction(b.dataset.action,[Number(b.dataset.id)]);return;}
  if(b.hasAttribute('data-remove-currency')){b.closest('.currency-edit-row').remove();updateCurrencySelects();return;}
  if(b.id==='add-currency'){$('#currency-rows').insertAdjacentHTML('beforeend',currencyEditorRow({code:'',name:'',symbol:'',decimals:2}));return;}
  if(b.id==='mobile-menu'){$('#sidebar').classList.toggle('open');return;}
  if(b.id==='refresh-ledger'){await busy(b,async()=>{try{await refresh();toast('团队账本已刷新');}catch(e){toast(e.message,true);}});return;}
  if(b.id==='profile-button'){$('#password-form').reset();$('#password-error').textContent='';$('#profile-dialog').showModal();return;}
  if(b.id==='add-member'){$('#member-form').reset();$('#member-error').textContent='';$('#member-dialog').showModal();return;}
  if(b.id==='save-draft'){await saveEntry('draft',b);return;}
  if(b.id==='logout'){await busy(b,async()=>{try{await api('/logout','POST');state.user=null;$('#profile-dialog').close();await showAuth(false);}catch(e){toast(e.message,true);}});return;}
  if(b.id==='refresh-fx'){await busy(b,async()=>{try{state.report=await api('/fx/refresh','POST');state.base=null;render();toast(state.report.complete?'已更新汇率快照':'部分汇率尚不可用',!state.report.complete);}catch(e){toast(e.message,true);}});return;}
  if(b.id==='generate-link'){await busy(b,async()=>{try{const data=await api('/telegram/link-code','POST');$('#link-code-result').innerHTML=`<div class="link-code">/start ${esc(data.code)}</div><p class="fx-caption">10 分钟内有效。请只发给机器人私聊；生成新码后，旧码立即失效。</p>`;}catch(e){toast(e.message,true);}});return;}
  if(b.id==='unlink-telegram'){if(!confirm('解除后，此 Telegram 账号将不能继续记账。确认解除？'))return;try{await api('/telegram/unlink','POST');await refresh();toast('已解除 Telegram 绑定');}catch(e){toast(e.message,true);}return;}
  if(b.hasAttribute('data-toggle-member')){
    const m=state.members.find(m=>m.id===Number(b.dataset.toggleMember));
    if(!confirm(`确认${m.active?'停用':'启用'}「${m.name}」？${m.active?'其登录会话将失效，历史账目会保留。':''}`))return;
    try{await api('/members/'+m.id,'PATCH',{active:m.active?0:1});await refresh();toast('已更新成员状态');}catch(e){toast(e.message,true);}return;
  }
  if(b.hasAttribute('data-reset-password')){
    const m=state.members.find(m=>m.id===Number(b.dataset.resetPassword));const value=prompt(`为 ${m.name} 设置新密码（10–128 位）。原登录会话将失效：`);if(value===null)return;
    try{await api('/members/'+m.id,'PATCH',{password:value});toast('密码已重置，请把新密码私下交给成员');}catch(e){toast(e.message,true);}return;
  }
});
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.matches('tr[data-entry]'))openDetail(Number(e.target.dataset.entry));});
document.addEventListener('input',event=>{
  const el=event.target;
  if(el.id==='search-entries'){state.filter.text=el.value;$('#ledger-results').innerHTML=ledgerResults();}
});
document.addEventListener('change',async event=>{
  const el=event.target;
  if(el.dataset.filter){state.filter[el.dataset.filter]=el.value;$('#ledger-results').innerHTML=ledgerResults();}
  if(el.dataset.select){const id=Number(el.dataset.select);el.checked?state.selected.add(id):state.selected.delete(id);if($('#batch-toolbar'))$('#batch-toolbar').innerHTML=batchToolbar();}
  if(el.id==='select-all'){$$('[data-select]').forEach(c=>{c.checked=el.checked;el.checked?state.selected.add(Number(c.dataset.select)):state.selected.delete(Number(c.dataset.select));});$('#batch-toolbar').innerHTML=batchToolbar();}
  if(el.name==='receipt'){$('#upload-label').textContent=el.files[0]?.name||'＋ 添加发票或凭证';}
  if(el.id==='detail-receipt'&&el.files[0]){
    const id=Number(el.dataset.id),file=el.files[0];el.disabled=true;
    try{if(file.size>6*1024*1024)throw new Error('凭证大小不能超过 6 MB');const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(',')[1]);r.onerror=()=>reject(new Error('文件读取失败'));r.readAsDataURL(file);});await api(`/entries/${id}/receipt`,'POST',{data,name:file.name});await refresh();$('#detail-dialog').close();await openDetail(id);toast('凭证已上传');}catch(e){el.disabled=false;toast(e.message,true);}
  }
  if(el.id==='report-base'){state.base=el.value;try{state.report=await api('/report?base='+encodeURIComponent(state.base));render();}catch(e){toast(e.message,true);}}
  if(el.hasAttribute('data-cur'))updateCurrencySelects();
});
function updateCurrencySelects() {
  const currencies=$$('.currency-edit-row').map(r=>({code:$('[data-cur=code]',r).value.trim().toUpperCase(),name:$('[data-cur=name]',r).value.trim()})).filter(c=>/^[A-Z]{3}$/.test(c.code));
  ['default-currency','reference-currency'].forEach(id=>{const el=$('#'+id);if(el){const value=el.value;el.innerHTML=options(currencies,c=>c.code,c=>c.code+' · '+c.name,value);}});
}
$('#auth-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=$('#auth-submit');await busy(button,async()=>{try{$('#auth-error').textContent='';const data=await api(state.setup?'/bootstrap':'/login','POST',Object.fromEntries(new FormData(event.target)));await enterApp(data);event.target.reset();}catch(e){$('#auth-error').textContent=e.message;}});
});
$('#entry-form').addEventListener('submit',async event=>{event.preventDefault();await saveEntry('pending',$('#save-entry'));});
$('#member-form').addEventListener('submit',async event=>{event.preventDefault();await busy($('button[type=submit]',event.target),async()=>{try{await api('/members','POST',Object.fromEntries(new FormData(event.target)));$('#member-dialog').close();await refresh();toast('成员账号已创建');}catch(e){$('#member-error').textContent=e.message;}});});
$('#password-form').addEventListener('submit',async event=>{event.preventDefault();await busy($('button[type=submit]',event.target),async()=>{try{const data=await api('/password','POST',Object.fromEntries(new FormData(event.target)));state.csrf=data.csrf;$('#profile-dialog').close();toast('密码已更新，其他登录会话已失效');}catch(e){$('#password-error').textContent=e.message;}});});
$('#action-form').addEventListener('submit',async event=>{event.preventDefault();await busy($('#action-confirm'),async()=>{try{await api('/entries/action','POST',{...state.action,note:event.target.elements.note.value});$('#action-dialog').close();$('#detail-dialog').close();state.selected.clear();await refresh();toast('账目状态已更新');}catch(e){$('#action-error').textContent=e.message;}});});
document.addEventListener('submit',async event=>{
  const form=event.target;
  if(form.id==='ai-form'){
    event.preventDefault();await busy($('button',form),async()=>{try{$('#ai-error').textContent='';const data=await api('/ai/parse','POST',{text:form.elements.text.value});openEntry(data.kind,data);toast('请核对识别出的金额、币种和日期，再确认提交');}catch(e){$('#ai-error').textContent=e.message;}});
  }
  if(form.id==='calculator-form'){
    event.preventDefault();await busy($('button',form),async()=>{try{$('#calculator-error').textContent='';const data=await api('/calculate','POST',{...Object.fromEntries(new FormData(form)),base:state.base||state.conf.reference_currency});$('#calculator-result').innerHTML=calcSummary(data,true);$('#plan-table').innerHTML=planTable(data);$('#fx-details').innerHTML=rateDetails(data)+reportWarning(data);}catch(e){$('#calculator-error').textContent=e.message;}});
  }
  if(form.id==='settings-form'){
    event.preventDefault();await busy($('button[type=submit]',form),async()=>{try{
      const body=Object.fromEntries(new FormData(form));body.telegram_allowed_chats=body.telegram_allowed_chats.split('\n').map(s=>s.trim()).filter(Boolean);
      body.currencies=$$('.currency-edit-row',form).map(row=>({code:$('[data-cur=code]',row).value.trim().toUpperCase(),name:$('[data-cur=name]',row).value.trim(),symbol:$('[data-cur=symbol]',row).value.trim(),decimals:Number($('[data-cur=decimals]',row).value)}));
      await api('/settings','PUT',body);state.base=null;await refresh();toast('团队设置已保存');
    }catch(e){$('#settings-error').textContent=e.message;}});
  }
});
window.addEventListener('hashchange',()=>{state.page=Object.hasOwn(PAGES,location.hash.slice(1))?location.hash.slice(1):'overview';state.selected.clear();$('#sidebar').classList.remove('open');render();window.scrollTo({top:0,behavior:'instant'});});
document.addEventListener('click',e=>{if($('#sidebar').classList.contains('open')&&!e.target.closest('#sidebar,#mobile-menu'))$('#sidebar').classList.remove('open');});
start();
