(function(){
  var t = localStorage.getItem('theme');
  if (!t) t = window.matchMedia('(prefers-color-scheme:light)').matches ? 'light' : 'dark';
  if (t === 'light') document.documentElement.dataset.theme = 'light';
})();
