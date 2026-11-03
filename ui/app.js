'use strict';
const $ = selector => document.querySelector(selector);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = value => value == null ? 'Не указана' : new Intl.NumberFormat('ru-RU', {maximumFractionDigits:2}).format(value / 1e6) + ' млн ₽';
const classes = {gab:'ГАБ',vacant:'Свободно',unknown:'Не определён',conflict:'Противоречие'};
let monitoring, publications, csrf = null;
let activeReport, selectedScenario = 'success';

function primary(obj) {
  return [...obj.listings].sort((a,b) => (b.payload.status === 'listed') - (a.payload.status === 'listed') || b.last_seen.localeCompare(a.last_seen))[0];
}
function check(obj) {
  if (obj.conflicts.length) return 'Противоречие источников';
  if (obj.listings.some(r => r.missed >= 2)) return 'Не виден 2 обхода · перепроверить';
  if (obj.listings.some(r => r.missed)) return 'Не виден при обходе';
  if (obj.listings.some(r => activeReport.sources[r.source]?.status !== 'ok')) return 'Сбой источника · данные сохранены';
  if (primary(obj).payload.classification === 'conflict') return 'Уточнить занятость';
  if (monitoring.reports[$('#report-day').value].merge_candidates.some(c => c.a === obj.object_id || c.b === obj.object_id)) return 'Кандидат на дубль';
  return 'Наблюдался · документы не проверены';
}
function renderMonitor() {
  activeReport = monitoring.reports[Number($('#report-day').value)];
  $('#report-title').textContent = `${activeReport.run_id.slice(-2)} ноября / 08:00 МСК`;
  document.querySelector('.monitor-top a[download]').href = `data/daily-report-${activeReport.run_id}.csv`;
  const failed = Object.entries(activeReport.sources).filter(([,s]) => s.status !== 'ok');
  $('#source-health').classList.toggle('warning', failed.length > 0);
  $('#source-health').innerHTML = Object.entries(activeReport.sources).map(([name,s]) => `<span class="health-chip"><i class="dot ${s.status === 'ok' ? 'green' : 'ochre'}"></i><strong>${escape(name)}</strong> ${s.status === 'ok' ? 'обход завершён' : 'сбой · HTTP 503 (имитация)'}</span>`).join('') + `<span class="health-note">${failed.length ? 'Данные источника со сбоем не обновлены. Это неполный отчёт.' : 'Полные снимки согласованных учебных фидов'}</span>`;
  const events = activeReport.events;
  const newCount = events.filter(e => e.kind === 'new').length;
  const changes = events.filter(e => /_changed$/.test(e.kind)).length;
  const questions = activeReport.objects.filter(o => o.conflicts.length || o.listings.some(r => r.missed || r.payload.classification === 'conflict')).length;
  $('#monitor-stats').innerHTML = [[activeReport.objects.length,'Объекты в базе'],[newCount,'Новые объекты'],[changes,'Изменения условий'],[questions,'На проверку']].map(([n,label]) => `<div class="stat"><strong>${n}</strong><span>${label}</span></div>`).join('');
  $('#object-detail').hidden = true;
  renderRows();
  const names = {new:'Новое',duplicate_linked:'Связь дубля',price_rub_changed:'Цена',status_changed:'Статус',not_seen:'Не наблюдается',source_failed:'Сбой',partial_source:'Частичный обход',identity_conflict:'Идентичность',volume_drop:'Объём'};
  $('#monitor-events').innerHTML = events.length ? events.map(e => {
    const d = e.detail;
    let text = `${d.source || ''} / ${d.listing_id || ''}`;
    if (e.kind === 'price_rub_changed') text += ` · ${money(d.before)} → ${money(d.after)} · уточнить условия`;
    if (e.kind === 'status_changed') text += ` · ${d.after === 'withdrawn_by_source' ? 'снято источником; причина не установлена' : d.after}`;
    if (e.kind === 'not_seen') text += ` · пропусков: ${d.consecutive_complete_misses}; продажа не подтверждена`;
    if (e.kind === 'source_failed') text = `${d.source}: ${d.reason}. Сведения сохранены с прежней датой.`;
    if (e.kind === 'new') text += ' · проверить применимость для клиента';
    if (e.kind === 'duplicate_linked') text += ' · совпал идентификатор помещения';
    return `<div class="event-item"><span>${escape(names[e.kind] || e.kind)}</span><span>${escape(text)}</span></div>`;
  }).join('') : '<p class="empty-state">Новых событий нет.</p>';
}
function renderRows() {
  $('#object-detail').hidden = true;
  const search = $('#object-search').value.toLocaleLowerCase('ru');
  const filter = $('#class-filter').value;
  const objects = activeReport.objects.filter(obj => {
    const p = primary(obj).payload;
    const matched = obj.listings.some(r => `${r.payload.address} ${r.listing_id}`.toLocaleLowerCase('ru').includes(search));
    const type = filter === 'all' || (filter === 'conflict' && obj.conflicts.length) || obj.listings.some(r => r.payload.classification === filter);
    return matched && type;
  });
  $('#object-rows').innerHTML = objects.length ? objects.map(obj => {
    const row = primary(obj), p = row.payload;
    const label = check(obj), warn = label !== 'Наблюдался · документы не проверены';
    const cls = p.classification;
    return `<tr><td><button class="address-button" data-object="${escape(obj.object_id)}">${escape(p.address)}</button><small>${escape(row.source)} / ${escape(row.listing_id)}${obj.listings.length > 1 ? ' · '+obj.listings.length+' источника' : ''}</small></td><td>${escape(p.area_m2)} м²</td><td>${escape(money(p.price_rub))}<small>${escape(row.last_seen.slice(8,10))}.${escape(row.last_seen.slice(5,7))} · ${escape(row.source)}</small></td><td><span class="pill ${cls === 'conflict' ? 'warn' : cls === 'unknown' ? 'grey' : ''}">${classes[cls]}</span></td><td><span class="check-text ${warn ? 'warn' : ''}">${escape(label)}</span></td><td><button class="row-arrow" data-object="${escape(obj.object_id)}" aria-label="Источники: ${escape(p.address)}">→</button></td></tr>`;
  }).join('') : '<tr><td colspan="6"><p class="empty-state">По этому запросу объектов нет. Измените фильтр или адрес.</p></td></tr>';
  $('#visible-count').textContent = `Показано ${objects.length} из ${activeReport.objects.length}`;
}
function openObject(id) {
  const obj = activeReport.objects.find(o => o.object_id === id);
  if (!obj) return;
  const row = primary(obj);
  $('#object-detail').innerHTML = `<div class="detail-heading"><div><h4>${escape(row.payload.address)}</h4><p>${escape(check(obj))}. Утверждения источников хранятся отдельно.</p></div><button id="close-detail" aria-label="Закрыть источники">×</button></div><div class="source-records">${obj.listings.map(r => `<article class="source-record"><h5>${escape(r.source)} / ${escape(r.listing_id)}</h5><time datetime="${escape(r.last_seen)}">Последнее наблюдение: ${escape(r.last_seen.replace('T',' '))}</time><p>${escape(r.payload.description)}</p><p>${escape(money(r.payload.price_rub))} · ${escape(classes[r.payload.classification])}<br>Статус: ${r.payload.status === 'listed' ? 'в объявлении активно' : 'снято источником'}<br>Пропусков полных обходов: ${r.missed}</p><p><code>${escape(r.payload.unit_cadastral_id || 'Нет точного ID помещения')}</code></p><p class="quiet">Учебная ссылка: <code>${escape(r.payload.url)}</code></p></article>`).join('')}</div>`;
  $('#object-detail').hidden = false;
  $('#close-detail').addEventListener('click', () => {$('#object-detail').hidden = true;});
  $('#object-detail').scrollIntoView({behavior:'smooth',block:'nearest'});
}

const scenarioText = {
  success:['Успешная обработка','Повторная постановка возвращает ту же задачу. submitted означает принятие; published появляется после отдельного учебного отчёта. Результат записан в advertising тестовой карточки.'],
  validation:['Неподтверждённые факты блокируют отправку','Цена не подтверждена, адрес конфликтует, согласование устарело. Возвращается 422 со списком полей. На внешней стороне не создано ни одного объекта.'],
  retry:['Очередь сохраняет повтор','Первая отправка получает временную ошибку. next_retry_at хранится в SQLite; ранний запуск не отправляет повтор. Демонстрация продвигает учебное время к сроку повтора, не ждёт реальные 30 секунд.'],
  timeout:['Неопределённый исход — сначала сверка','Тестовый приёмник сохранил объект, но ответ потерян. При повторном запуске модуль находит его по постоянному Id и получает статус. Второй отправки и второго объекта нет.'],
  conflict:['409 не должен приводить к повторной публикации','Устаревшая версия сначала блокирует постановку. После перечитывания отправка проходит. Отдельный 409 при записи результата решается перечитыванием карточки и обновлением advertising. Внешний объект остаётся один.'],
  update:['Новая цена — прежний внешний Id','После первого успеха цена меняется на 23,5 млн ₽ (учебная), содержание согласуется заново. Обновление использует прежний Id. Изменение служебной версии из-за advertising само по себе не создаёт новую публикацию.'],
  rejected:['Постоянную ошибку нельзя лечить повтором','Тестовый адаптер отклоняет категорию. В GP OS возвращается понятная ошибка CATEGORY_REJECTED. Автоматический повтор отключён; сначала нужно исправить mapping.']
};
function renderPublication(result, live=false) {
  const final = result.final;
  const bad = Boolean(result.error || (final && ['rejected','manual_review'].includes(final.status)));
  const hasSend = result.frames.some(f => f.step === 'send');
  const hasPublished = final?.status === 'published';
  const hasSync = final?.sync_status === 'synced';
  const steps = [
    ['Карточка получена','UUID, версия 7, факты, описание и asset_id',true,false],
    ['Проверка и согласование',result.error ? '422 · поля требуют исправления' : 'Факты подтверждены в учебной модели',!result.error,!!result.error],
    ['Очередь / стабильный Id','UNIQUE: объект + канал + аккаунт',!result.error,false],
    ['Отправка и отчёт',hasPublished ? 'submitted → published (тестовый отчёт)' : final?.status === 'rejected' ? 'Отклонено тестовым адаптером' : 'До отправки не дошло',hasSend,final?.status === 'rejected'],
    ['Возврат в GP OS',hasSync ? 'advertising обновлён с expectedVersion' : 'Возвращён список ошибок',hasSync,false]
  ];
  $('#pipeline').innerHTML = steps.map(([title,detail,done,error],i) => `<div class="pipeline-step ${error ? 'error' : done ? '' : 'pending'}"><span class="step-marker">${error ? '!' : done ? '✓' : i+1}</span><div><strong>${escape(title)}</strong><small>${escape(detail)}</small></div></div>`).join('');
  const card = $('#publication-result');
  card.classList.toggle('error-result',bad);
  const title = result.error ? 'Нужна проверка' : final.status === 'rejected' ? 'Отказ категории' : hasPublished ? 'Обработано' : final.status;
  const error = result.error || final?.error;
  card.innerHTML = `<p class="eyebrow">Результат обработки</p><h3>${escape(title)}</h3><span class="pill ${bad ? 'warn' : ''}">${escape(result.error ? result.http_status + ' / ' + result.error.code : final.status + ' / ' + final.sync_status)}</span>${error ? `<p>${escape(error.message)}</p><code>${escape(error.fields?.join('\n') || error.code)}</code>` : `<p>Статус возвращён в учебную карточку. Ссылка ниже фиктивная и никуда не ведёт.</p><code>${escape(final.url)}</code>`}<div class="result-count"><strong>${result.remote_objects}</strong>объект${result.remote_objects === 1 ? '' : 'ов'} в тестовом приёмнике</div><p class="small-note">Авито и production GP OS не вызывались.</p>`;
  $('#scenario-explanation').innerHTML = `<strong>${escape(scenarioText[selectedScenario][0])}.</strong> ${escape(scenarioText[selectedScenario][1])}`;
  $('#publication-json').textContent = JSON.stringify(result,null,2);
  $('#execution-mode').textContent = live ? 'Выполнено сейчас: локальный Python + SQLite, внешний адаптер fake.' : 'Сохранённый прогон Python. Это запись результата, не новая отправка.';
}
async function runScenario() {
  if (!csrf) return;
  const button = $('#run-scenario');
  button.disabled = true;
  $('#execution-mode').textContent = 'Выполняется локальный сценарий…';
  try {
    const response = await fetch('/api/demo',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({scenario:selectedScenario})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || data.error || `HTTP ${response.status}`);
    renderPublication(data,true);
  } catch(error) {
    $('#execution-mode').textContent = `Локальный запуск не выполнен: ${error.message}. Доступен сохранённый прогон.`;
  } finally {button.disabled = false;}
}

async function init() {
  try {
    const [m,p] = await Promise.all(['data/monitoring.json','data/publication.json'].map(async path => {const r = await fetch(path);if (!r.ok) throw new Error(path);return r.json();}));
    monitoring=m; publications=p;
    renderMonitor();renderPublication(publications.success);
    $('#report-day').addEventListener('change',renderMonitor);
    $('#object-search').addEventListener('input',renderRows);
    $('#class-filter').addEventListener('change',renderRows);
    $('#object-rows').addEventListener('click',event => {const button=event.target.closest('[data-object]');if(button)openObject(button.dataset.object);});
    $('#scenario-select').addEventListener('change',() => {selectedScenario=$('#scenario-select').value;renderPublication(publications[selectedScenario]);});
    $('#run-scenario').addEventListener('click',runScenario);
    const local = ['127.0.0.1','localhost'].includes(location.hostname);
    if (local) {
      try {const response=await fetch('/api/session');if(response.ok)csrf=(await response.json()).csrf;} catch {}
    }
    if (!csrf) {$('#run-scenario').disabled=true;$('#run-scenario').textContent='Запуск: python -m src.server';$('#run-scenario').title='На статическом хостинге доступны сохранённые прогоны. Для нового выполнения нужен локальный сервер.';}
    try {const r=await fetch('data/verification.json');if(r.ok){const v=await r.json();$('#test-count').textContent=`${v.tests_run} тестов · ${v.success ? 'OK' : 'есть ошибки'}`;}} catch {}
  } catch(error) {
    $('#source-health').textContent='Не удалось загрузить данные. Запустите python -m tools.build, затем python -m src.server и откройте HTTP-адрес (не file://).';
    $('#execution-mode').textContent=`Ошибка загрузки: ${error.message}`;
    $('#run-scenario').disabled=true;
  }
}
init();
