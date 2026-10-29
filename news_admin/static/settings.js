// 系统设置页：复制 RSS 地址、危险操作二次确认
document.querySelectorAll('[data-copy-input]').forEach((btn) => btn.addEventListener('click', async () => {
  const input = document.querySelector(btn.dataset.copyInput);
  if (!input) return;
  try { await navigator.clipboard.writeText(input.value); }
  catch { input.select(); document.execCommand('copy'); }
  const label = btn.textContent;
  btn.textContent = '已复制';
  setTimeout(() => { btn.textContent = label; }, 2000);
}));

document.querySelectorAll('form[data-confirm]').forEach((form) => form.addEventListener('submit', (event) => {
  if (!window.confirm(form.dataset.confirm)) event.preventDefault();
}));
