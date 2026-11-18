const el = id => document.getElementById(id);
async function fixture(name) {
  const response = await fetch('../contracts/examples/' + name + '.json');
  if (!response.ok) throw Error(name);
  return response.json();
}

async function start() {
  const [room, status, access] = await Promise.all(['room','status','access'].map(fixture));
  const seat = room.seats[0];
  if (seat.nodeId !== status.nodeId) throw Error('nodeId');
  const monitors = status.session.monitors;
  const frames = monitors.map((_, index) => 1000 * index);
  let request = null;
  el('room').textContent = 'Аудитория ' + room.roomId;
  el('seat').textContent = seat.seatId + ' · ' + seat.nodeId;
  const labels = {moving:'Живое превью', unchanged:'Экран неподвижен', stale:'Кадр устарел', logout:'Нет графической сессии', offline:'ПК вне сети'};

  function button(label, action) {
    const button = document.createElement('button');
    button.textContent = label;
    button.onclick = action;
    el('actions').append(button);
  }

  function open(target) {
    request = {...access, nodeId:seat.nodeId, sessionId:status.session.sessionId,
      layoutRevision:status.session.layoutRevision, mode:'view', target};
    render();
  }

  function render() {
    const state = el('scenario').value;
    const screens = el('screens');
    screens.replaceChildren();
    el('actions').replaceChildren();
    el('status').textContent = labels[state];
    el('title').textContent = '';
    if (state === 'offline' || state === 'logout') {
      request = null;
      frames.fill(0);
      screens.style.aspectRatio = 'auto';
      return;
    }
    const target = request?.target;
    const visible = monitors.filter(m => target?.scope !== 'monitor' || target.monitorId === m.monitorId);
    const x = Math.min(...visible.map(m => m.x));
    const y = Math.min(...visible.map(m => m.y));
    const w = Math.max(...visible.map(m => m.x + m.width)) - x;
    const h = Math.max(...visible.map(m => m.y + m.height)) - y;
    screens.style.aspectRatio = w + '/' + h;
    screens.classList.toggle('large', Boolean(request));
    screens.style.width = request ? 'min(100%, ' + (65*w/h) + 'vh)' : '';
    el('title').textContent = target?.monitorId ?? (request ? 'Весь рабочий стол' : 'Мониторы места');
    for (const m of visible) {
      const index = monitors.indexOf(m);
      const panel = document.createElement('div');
      panel.className = 'screen';
      panel.dataset.index = index;
      Object.assign(panel.style, {left:(m.x-x)/w*100+'%', top:(m.y-y)/h*100+'%',
        width:m.width/w*100+'%', height:m.height/h*100+'%'});
      const counter = document.createElement('b');
      counter.textContent = frames[index];
      panel.append(m.monitorId, counter);
      screens.append(panel);
    }
    if (request) button('Вернуться в грид', () => { request = null; render(); });
    else {
      for (const m of monitors) button('Открыть '+m.monitorId, () => open({scope:'monitor',monitorId:m.monitorId}));
      button('Весь рабочий стол', () => open({scope:'desktop'}));
    }
  }
  el('scenario').onchange = render;
  render();
  setInterval(() => {
    if (el('scenario').value !== 'moving') return;
    frames.forEach((_, i) => frames[i] += i+1);
    for (const panel of el('screens').children) panel.lastChild.textContent = frames[panel.dataset.index];
  }, 150);
}
start().catch(error => { el('seat').textContent = 'Ошибка загрузки примеров: ' + error.message; });
