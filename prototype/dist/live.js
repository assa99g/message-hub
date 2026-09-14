'use strict';
const $ = s => document.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function acceptSession() {
  const token = new URLSearchParams(location.hash.slice(1)).get('session');
  if (token) { sessionStorage.setItem('message-hub-session', token); history.replaceState(null, '', '#sources'); }
}
acceptSession();
let statusData = null, selectedAccount = '', offset = 0, renderVersion = 0;
const routes = {today:'Сегодня', inbox:'Входящие', tasks:'Задачи', calendar:'Календарь', questions:'Уточнения', sources:'Источники', settings:'Настройки'};
const date = ms => ms ? new Date(ms).toLocaleString('ru-RU', {timeZone:'Europe/Madrid'}) : 'Ещё не проверялся';
const btn = (text, action, primary=false) => `<button class="btn ${primary?'primary':''}" data-action="${action}">${text}</button>`;
function toast(text) { $('#toast').textContent=text; $('#toast').classList.add('show'); clearTimeout(toast.timer); toast.timer=setTimeout(()=>$('#toast').classList.remove('show'),5000); }
async function api(path, body) {
  const response=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:{'X-Message-Hub-Session':sessionStorage.getItem('message-hub-session')||'','Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});
  const data=await response.json(); if(!response.ok) throw new Error(data.error || 'Не удалось выполнить действие.'); return data;
}
const heading = (title, subtitle, action='') => `<div class="page-heading"><div><div class="eyebrow">${esc(new Date().toLocaleDateString('ru-RU',{timeZone:'Europe/Madrid',day:'numeric',month:'long',year:'numeric'}))}</div><h1>${title}</h1><p class="subtitle">${subtitle}</p></div>${action}</div>`;
function sources() {
  const s=statusData;
  return heading('Источники','Подключите Gmail-ящики по одному. Их письма появятся в общем списке.')+
    (!s.configured?`<section class="panel"><div class="panel-head"><h2>1. Настройка подключения Google</h2></div><div class="panel-body"><p>Один раз подготовьте приложение Message Hub в Google Cloud. Настройка общая для всех ящиков.</p><ol><li>Создайте проект и включите Gmail API.</li><li>В Google Auth Platform укажите название Message Hub и адрес поддержки.</li><li>Для личных ящиков выберите External; в режиме Testing добавьте подключаемые адреса в Test users.</li><li>В Data Access добавьте разрешение gmail.readonly.</li><li>В Clients создайте OAuth-клиент типа Desktop app и скачайте JSON.</li></ol><p><a class="link-button" href="https://console.cloud.google.com/" target="_blank" rel="noreferrer">Открыть Google Cloud →</a></p><div class="form-row"><label for="client-file">Файл настройки приложения Google (JSON)</label><input id="client-file" type="file" accept="application/json,.json"></div><p class="form-hint">Файл сохраняется локально. Пароли от почты вводятся только на странице Google.</p></div></section>`:'')+
    `<section class="panel"><div class="panel-head"><h2>${s.configured?'Подключить ящик':'2. Подключение ящиков'}</h2></div><div class="panel-body"><p>Google откроется в обычном браузере. Выберите ящик и разрешите чтение. Повторите для остальных аккаунтов.</p><div class="actions"><button class="btn primary" data-action="connect" ${!s.configured||s.auth_status==='waiting'?'disabled':''}>${s.auth_status==='waiting'?'Ожидаем вход в Google…':'Добавить Gmail-ящик'}</button>${btn('Проверить почту','sync')}</div><p id="auth-status" role="status">${s.auth_status==='connected'?'Ящик подключён. Можно добавить следующий или проверить почту.':s.auth_status==='waiting'?'Завершите вход в окне Google. Ожидание — до 5 минут.':esc(s.auth_error)}</p><p class="form-hint">Собираются новые входящие письма после подключения, включая архивные папки, спам и корзину. Отправленные и черновики исключены. Вложения не обрабатываются.</p></div></section>`+
    `<div class="source-grid">${s.accounts.map(a=>`<article class="source-card"><div class="source-card-head"><span class="source-icon">@</span><h3>${esc(a.email)}</h3></div><span class="badge ${a.error?'amber':'green'}">${a.error?'Нужна проверка':'Подключён'}</span><p>Сбор с ${date(a.connected_ms)}</p><p>Последняя проверка: ${date(a.last_sync_ms)}</p>${a.error?`<p role="alert">${esc(a.error)}</p>`:''}</article>`).join('')||'<div class="empty">Пока нет подключённых ящиков.</div>'}</div><p class="footer-note">Проверка каждые ${s.interval} мин, пока программа запущена. ${s.syncing?'Проверяем почту…':''}</p>`;
}
async function inbox() {
  const rows=await api(`messages?account=${encodeURIComponent(selectedAccount)}&offset=${offset}`);
  return heading('Входящие','Новые письма из подключённых Gmail-ящиков.',btn('Проверить сейчас','sync'))+
    `<div class="toolbar"><label for="mailbox">Ящик</label><select id="mailbox"><option value="">Все ящики</option>${statusData.accounts.map(a=>`<option value="${esc(a.id)}" ${a.id===selectedAccount?'selected':''}>${esc(a.email)}</option>`).join('')}</select></div><section class="panel">${rows.map(m=>`<article class="message-row"><span class="source-icon">@</span><div class="row-content"><div class="row-meta"><span>${esc(m.sender)}</span><span>${date(m.received_ms)}</span></div><button class="row-title" data-message="${m.id}">${esc(m.subject)}</button><div class="tags"><span class="badge">${esc(m.email)}</span><span class="badge">${m.opened_ms?'Открыто в Message Hub':'Новое'}</span></div></div></article>`).join('')||'<div class="empty">Писем пока нет. После подключения сюда попадут только новые письма.</div>'}</section><div class="actions"><button class="btn" data-action="previous" ${offset===0?'disabled':''}>Предыдущие</button><span>Страница ${offset/100+1}</span><button class="btn" data-action="next" ${rows.length<100?'disabled':''}>Следующие</button></div><p class="footer-note">ИИ-анализ ещё не подключён. Открытие учитывается только здесь; статус прочтения в Gmail не меняется.</p>`;
}
function today() {
  const s=statusData.stats;
  return heading('Почта — в одном месте','Статистика накапливается с момента подключения ящиков.',btn('Проверить сейчас','sync'))+`<section class="stats">${[['Ящиков',statusData.accounts.length],['Сохранено писем',s.messages],['Открыто в Message Hub',s.opened],['Ещё не открыто',s.unread]].map(([label,value])=>`<div class="stat"><div class="stat-label">${label}</div><div class="stat-value">${value}</div></div>`).join('')}</section><section class="panel"><div class="panel-body"><p>Письма хранятся на этом компьютере. Откройте «Входящие» для чтения или «Источники», чтобы добавить следующий ящик.</p><p class="note">Анализ ИИ, задачи и календарь подключим на следующих этапах.</p></div></section>`;
}
async function render() {
  acceptSession();
  const version=++renderVersion;
  const view=routes[location.hash.slice(1)]?location.hash.slice(1):'sources';
  $('#nav').innerHTML=Object.entries(routes).map(([key,label])=>`<a class="nav-link ${key===view?'active':''}" href="#${key}" ${key===view?'aria-current="page"':''}>${label}</a>`).join('');
  $('#crumb').textContent=routes[view];
  try {
    statusData=await api('status');
    let html;
    if(view==='sources') html=sources(); else if(view==='inbox') html=await inbox(); else if(view==='today') html=today();
    else if(view==='settings') html=heading('Настройки','Локальное хранение и сбор почты.')+`<section class="panel"><div class="panel-body"><p>Проверка: каждые ${statusData.interval} мин. Время отображается для Europe/Madrid.</p><p>Разрешения Gmail хранятся в Связке ключей macOS, тексты и история открытий — в локальной базе SQLite.</p><p>Автозапуск, автоматические резервные копии и перенос через интерфейс ещё не реализованы.</p></div></section>`;
    else html=heading(routes[view],'Этот раздел подключим на следующем этапе.')+'<section class="panel"><div class="empty">Пока работают подключение Gmail, сбор, чтение и подсчёт писем.</div></section>';
    if(version===renderVersion) $('#main').innerHTML=html;
  } catch(error) { if(version===renderVersion) $('#main').innerHTML=`<div class="note" role="alert">${esc(error.message)}</div>`; }
}
document.addEventListener('click',async e=>{
  const action=e.target.closest('[data-action]')?.dataset.action;
  const message=e.target.closest('[data-message]')?.dataset.message;
  try {
    if(action==='connect') { await api('connect',{}); await render(); }
    if(action==='sync') { await api('sync',{}); toast('Проверка запущена.'); await render(); }
    if(action==='previous'||action==='next') { offset=Math.max(0,offset+(action==='next'?100:-100)); await render(); }
    if(action==='close') $('#dialog').close();
    if(message) {
      const m=await api('open',{id:Number(message)});
      const link=`https://mail.google.com/mail/u/?authuser=${encodeURIComponent(m.email)}#all/${encodeURIComponent(m.thread_id)}`;
      $('#dialog-content').innerHTML=`<div class="dialog-header"><h2>${esc(m.subject)}</h2><button class="close" data-action="close" aria-label="Закрыть">×</button></div><div class="dialog-body"><p class="row-meta">${esc(m.sender)} · ${esc(m.email)}</p><p>${date(m.received_ms)}</p><div class="original">${esc(m.body||'В письме нет доступного текстового содержимого.').replace(/\n/g,'<br>')}</div><p><a class="link-button" href="${esc(link)}" target="_blank" rel="noreferrer">Открыть в Gmail →</a></p></div>`;
      $('#dialog').showModal(); await render();
    }
  } catch(error) { toast(error.message); }
});
document.addEventListener('change',async e=>{
  if(e.target.id==='mailbox') { selectedAccount=e.target.value; offset=0; await render(); }
  if(e.target.id==='client-file') {
    try { const file=e.target.files[0]; if(!file)return; if(file.size>32768)throw new Error('Выберите JSON-файл OAuth-клиента Google.'); await api('config',JSON.parse(await file.text())); toast('Настройка сохранена. Теперь добавьте ящики.'); await render(); }
    catch(error) { toast(error.message); }
  }
});
window.addEventListener('hashchange',render);
setInterval(async()=>{
  if(document.hidden||!statusData) return;
  try { const fresh=await api('status'); if(JSON.stringify(fresh)!==JSON.stringify(statusData)) await render(); } catch { /* Keep displayed data if the server is temporarily unavailable. */ }
},3000);
render();
