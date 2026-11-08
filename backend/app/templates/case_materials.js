// Only fixed reader behavior lives here. Case content is escaped HTML text.
(() => {
  const chapters = [...document.querySelectorAll('.chapter')];
  const links = [...document.querySelectorAll('.contents > a')];
  let sourceTrigger;
  function navigate(id, focus = true) {
    const target = document.getElementById(id);
    const chapter = target?.closest('.chapter');
    if (!chapter) return;
    chapters.forEach(item => { item.hidden = item !== chapter; });
    links.forEach(link => {
      if (link.hash === `#${chapter.id}`) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
    let parent = target;
    while (parent && parent !== chapter) {
      if (parent.tagName === 'DETAILS') parent.open = true;
      parent = parent.parentElement;
    }
    if (focus) {
      chapter.querySelector('h2').focus({ preventScroll: true });
      target.scrollIntoView({ block: 'start' });
    }
  }
  document.addEventListener('click', event => {
    const source = event.target.closest('[data-source]');
    if (source) {
      sourceTrigger = source;
      document.querySelectorAll('.source-panel').forEach(panel => { panel.hidden = panel.id !== source.dataset.source; });
      document.querySelector('.source-placeholder').hidden = true;
      document.body.classList.add('show-sources');
      const panel = document.getElementById(source.dataset.source);
      panel?.querySelector('h3')?.setAttribute('tabindex', '-1');
      panel?.querySelector('h3')?.focus({ preventScroll: true });
    }
    if (event.target.closest('#close-sources')) {
      document.body.classList.remove('show-sources');
      sourceTrigger?.focus({ preventScroll: true });
    }
    const anchor = event.target.closest('a[href^="#"]');
    if (anchor) {
      event.preventDefault();
      navigate(anchor.hash.slice(1));
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.body.classList.contains('show-sources')) {
      document.body.classList.remove('show-sources');
      sourceTrigger?.focus({ preventScroll: true });
    }
  });
  navigate('overview', false);
})();
