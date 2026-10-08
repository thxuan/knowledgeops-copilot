/* Offline enhancement only: all navigation and content also work without JavaScript. */
(() => {
  const $ = (selector) => document.querySelector(selector);
  const base = document.body.dataset.base || '';
  const menu = $('.menu-button');
  const narrow = window.matchMedia('(max-width:760px)');
  function syncMenu() {
    const open = document.body.classList.contains('nav-open');
    $('#sidebar').inert = narrow.matches && !open;
    menu?.setAttribute('aria-expanded', String(open));
    menu?.setAttribute('aria-label', open ? '关闭导航' : '打开导航');
  }
  narrow.addEventListener('change', syncMenu);
  syncMenu();
  menu?.addEventListener('click', () => {
    document.body.classList.toggle('nav-open');
    syncMenu();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      document.body.classList.remove('nav-open');
      syncMenu();
    }
  });
  const dialog = $('#search-dialog');
  const input = $('#global-search');
  const resultBox = $('#search-results');
  function search() {
    const query = input.value.trim().toLowerCase();
    const words = query.split(/\s+/).filter(Boolean);
    const entries = (window.ATLAS_SEARCH || []).filter(item => words.every(word =>
      `${item.title} ${item.description} ${item.keywords}`.toLowerCase().includes(word)));
    resultBox.replaceChildren();
    const summary = document.createElement('p');
    summary.className = 'search-summary';
    summary.textContent = query ? `找到 ${entries.length} 项结果` : '按流程或术语探索，也可以搜索文件名与函数名';
    resultBox.append(summary);
    entries.slice(0, query ? 40 : 8).forEach(item => {
      const link = document.createElement('a');
      link.href = base + item.url;
      const label = document.createElement('small');
      const title = document.createElement('strong');
      const description = document.createElement('span');
      label.textContent = item.label;
      title.textContent = item.title + ' ↗';
      description.textContent = item.description;
      link.append(label, title, description);
      link.addEventListener('click', () => dialog.close());
      resultBox.append(link);
    });
    if (!entries.length) {
      const empty = document.createElement('p');
      empty.textContent = '没有找到匹配项。试试中文概念、英文缩写或函数名。';
      resultBox.append(empty);
    }
  }
  function openSearch() { if (!dialog.open) dialog.showModal(); search(); input.focus(); }
  $('.search-trigger')?.addEventListener('click', openSearch);
  input?.addEventListener('input', search);
  document.addEventListener('keydown', event => {
    const editing = /INPUT|TEXTAREA|SELECT/.test(event.target.tagName) || event.target.isContentEditable;
    if (event.key === '/' && !editing && !event.ctrlKey && !event.metaKey) {
      event.preventDefault(); openSearch();
    }
  });
  dialog?.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  document.querySelectorAll('[data-focus]').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('[data-focus]').forEach(b => {
      b.classList.toggle('selected', b === button); b.setAttribute('aria-pressed', String(b === button));
    });
    document.querySelectorAll('[data-lane]').forEach(lane => {
      lane.classList.toggle('muted-lane', button.dataset.focus !== 'all' && lane.dataset.lane !== button.dataset.focus);
      lane.classList.toggle('focused-lane', lane.dataset.lane === button.dataset.focus);
    });
  }));
  function localFilter(inputId, itemSelector, countId, emptyId) {
    const control = $(inputId);
    if (!control) return;
    const apply = () => {
      const query = control.value.trim().toLowerCase();
      let count = 0;
      document.querySelectorAll(itemSelector).forEach(item => {
        item.hidden = !item.dataset.search.includes(query);
        if (!item.hidden) count++;
      });
      $(countId).textContent = `${count} 项`;
      $(emptyId).hidden = count !== 0;
    };
    control.addEventListener('input', apply); apply();
  }
  localFilter('#term-filter','.term-card','#term-count','#term-empty');
  localFilter('#code-filter','.code-index tbody tr','#code-count','#code-empty');
  document.querySelectorAll('.copy-button').forEach(button => button.addEventListener('click', async () => {
    const code = button.closest('.example').querySelector('code');
    try {
      await navigator.clipboard.writeText(code.textContent);
      button.textContent = '已复制';
    } catch {
      const range = document.createRange(); range.selectNodeContents(code);
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      button.textContent = '已选中，请复制';
    }
  }));
  $('.back-button')?.addEventListener('click', () => {
    if (history.length > 1) history.back(); else location.href = base + 'code-index.html';
  });
})();
