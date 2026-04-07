let toastTimer;

function showToast(msg, type = 'info') {
  const t = document.getElementById('toast');
  const icon  = type === 'success' ? '✓' : type === 'error' ? '✕' : 'ℹ';
  const color = type === 'success' ? 'var(--teal)' : type === 'error' ? 'var(--red)' : 'var(--blue)';
  t.innerHTML = '<span style="color:' + color + '">' + icon + '</span> ' + msg;
  t.className = 'show ' + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ''; }, 4000);
}
