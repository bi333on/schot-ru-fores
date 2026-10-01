// Автоотправка формы при выборе значения в select
document.addEventListener('change', function (e) {
  var el = e.target;
  if (el.matches('select.js-auto-submit')) {
    el.form && el.form.submit();
  }
});

// Подтверждение перед отправкой формы
document.addEventListener('submit', function (e) {
  var form = e.target;
  if (form.matches('form.js-confirm')) {
    var msg = form.getAttribute('data-confirm') || 'Вы уверены?';
    if (!window.confirm(msg)) {
      e.preventDefault();
      return false;
    }
  }
});
